"""Verified public stages survive research handoffs, never bare/model state."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from shu import maa_agents
from shu.core import TaskEngine
from shu.providers import ProviderConfig
from shu.research_tools import ResearchTools
from shu.workflow_store import WorkflowStore


def call(name, **arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': name, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class LocalClient:
    def __init__(self, config):
        self.config, self.responses = config, []

    def complete(self, messages, **kwargs):
        return self.responses.pop(0)


class ResearchStageContinuityTests(unittest.TestCase):
    def setUp(self):
        staging = Path(__file__).resolve().parents[2] / '.pet-temp'
        staging.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=staging)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        self.current = self.store.submit('查找GH-1作业JSON，不匹配不导入', {
            'goal': '查找GH-1作业JSON，不匹配不导入',
            'task_spec': {'kind': 'lookup', 'stage_code': 'GH-1'}})
        self.downloads, self.level_calls = [], []
        def levels(query, directory):
            self.level_calls.append(query)
            return {'source': 'https://prts.maa.plus/arknights/level', 'raw_sha256': 'a' * 64,
                'levels': [{'code': 'GH-1', 'stage_id': 'offline_stage', 'level_id': 'offline_level',
                            'match': 'exact_code_or_id'}]}
        def transport(req, **kwargs):
            self.assertTrue(req.full_url.startswith('https://prts.maa.plus/copilot/get/'))
            self.downloads.append(req.full_url)
            self.assertIsNone(req.get_header('Authorization'))
            return json.dumps({'stage_name': 'offline_stage', 'doc': {'title': 'offline'},
                'opers': [{'name': '离线干员', 'skill': 1, 'requirements': {'elite': 2}}],
                'actions': [{'type': 'Deploy'}]}).encode()
        def tools(config, directory, *, public_cache_root=None):
            self.assertEqual(public_cache_root, self.root / 'public-cache')
            config.resolve_key = lambda: self.fail('Public evidence must never read a key')
            return ResearchTools(config, directory, transport=transport, level_query=levels,
                                 public_cache_root=public_cache_root)
        page = {'source': 'https://prts.maa.plus/copilot/query?levelKeyword=offline_stage',
            'total': 2, 'has_more': False, 'raw_count': 2, 'filter_status': 'exact_stage_id',
            'stage_match_verified': True, 'candidates': [
                {'id': identifier, 'title': 'offline', 'stage_name': 'offline_stage', 'available': True,
                 'source': f'https://prts.maa.plus/copilot/get/{identifier}',
                 'mystery_code': f'prts://{identifier}', 'groups': [],
                 'operators': [{'name': '离线干员', 'skill': 1, 'requirements': {'elite': 2}}]}
                for identifier in (101, 102)]}
        for item in (patch.object(maa_agents, 'DeepSeekClient', LocalClient),
                     patch.object(maa_agents, 'ResearchTools', side_effect=tools),
                     patch('shu.research_tools.query_copilots', return_value=page)):
            item.start(); self.addCleanup(item.stop)
        self.roles = maa_agents.MaaRoleAgents(self.store, self.root, ProviderConfig(), ProviderConfig(),
                                             None, lambda: False, lambda: False)
        self.addCleanup(self.roles.close)
        self.cancel = threading.Event()

    def context(self, key, prior=None):
        current = self.store.current(self.current['task_id'])
        dispatch = self.store.ensure_dispatch(current['task_id'], 'research', key,
                                               {'contract': current['contract'], 'task': key})
        return {**{key: current[key] for key in ('task_id', 'epoch', 'revision', 'contract')},
                'dispatch_id': dispatch['dispatch_id'], 'role_results': {'research': prior} if prior else {}}

    def first_research(self, *, assess=False):
        context = self.context('first')
        self.roles.clients['research'].responses = [call('research_candidates', stage_or_keyword='GH-1'),
            call('research_download', url='https://prts.maa.plus/copilot/get/101')]
        if assess:self.roles.clients['research'].responses.append(call('assess_candidate',copilot_id='101'))
        self.roles.clients['research'].responses.append(call('finish_role', status='researched', summary='offline tool facts'))
        result = self.roles.handlers['research'](context, self.cancel)
        self.assertEqual(len(result['verified_copilots']), 1)
        self.store.finish_dispatch(context['dispatch_id'], result)
        return result

    def second_download(self, prior):
        context = self.context('second', prior)
        self.roles.clients['research'].responses = [call('research_download', url='https://prts.maa.plus/copilot/get/102'),
            call('finish_role', status='researched', summary='second actual download')]
        return context, self.roles.handlers['research'](context, self.cancel)

    def test_new_dispatch_reuses_verified_stage_mapping_for_another_discovered_candidate(self):
        prior = self.first_research()
        context, result = self.second_download(prior)
        self.assertEqual([r['copilot_id'] for r in result['verified_copilots']], ['101', '102'])
        self.assertEqual(self.level_calls, ['GH-1'])
        record = self.roles._read_ref(result['candidate_refs'][-1])
        self.assertEqual(record['research_dispatch_id'], context['dispatch_id'])
        first = self.roles._read_ref(prior['candidate_refs'][0])
        self.assertEqual(record['goal_stage'], first['goal_stage'])

    def test_unregistered_bare_goal_stage_cannot_become_handoff_evidence(self):
        prior = self.first_research()
        original = self.roles._read_ref(prior['candidate_refs'][0])
        prior = copy.deepcopy(prior)
        prior['goal_stages'] = {'offline_stage': original['goal_stage']}
        prior['candidate_refs'] = []; prior['verified_copilots'] = []
        _, result = self.second_download(prior)
        self.assertEqual(result['verified_copilots'], [])
        self.assertTrue(any(error['tool'] == 'research_download' for error in result.get('tool_errors', [])))

    def test_tampered_old_full_document_cannot_restore_stage_mapping(self):
        prior = self.first_research()
        Path(prior['verified_copilots'][0]['path']).write_bytes(b'changed')
        _, result = self.second_download(prior)
        self.assertEqual([r['copilot_id'] for r in result['verified_copilots']], ['101'])
        self.assertTrue(any(error['tool'] == 'research_download' for error in result.get('tool_errors', [])))

    def roster(self, elite, suffix):
        return self.roles.rosters.facts.publish('local-default',[
            {'name':'离线干员','owned':True,'rarity':6,'elite':elite}],False,
            {'kind':'user-roster-spreadsheet','observed_at':1000,'evidence_ref':'fixture:roster-'+suffix},
            explicit_ownership=True)

    def test_repeated_stage_reuses_registered_json_and_reassesses_current_roster(self):
        old=self.roster(2,'old');prior=self.first_research(assess=True)
        current=self.roster(2,'new')
        self.assertNotEqual(old['box_revision'],current['box_revision'])
        context=self.context('recheck-current-box',prior)
        self.roles.clients['research'].responses=[call('research_stage_jobs',stage_code='GH-1'),
            call('finish_role',status='researched',summary='current roster rechecked')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(self.downloads,['https://prts.maa.plus/copilot/get/101'])
        self.assertEqual(self.level_calls,['GH-1'])
        self.assertEqual(result['assessments']['101']['box_revision'],current['box_revision'])
        self.assertEqual(result['assessments']['101']['classification'],'satisfied')
        self.assertEqual(result['candidate_refs'],prior['candidate_refs'])

    def test_reuse_never_carries_a_previously_satisfied_roster_after_elite_changed(self):
        self.roster(2,'old');prior=self.first_research(assess=True)
        current=self.roster(1,'new')
        context=self.context('elite-changed',prior)
        self.roles.clients['research'].responses=[call('research_stage_jobs',stage_code='GH-1'),
            call('finish_role',status='unknown',summary='elite now fails')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(result['assessments']['101']['box_revision'],current['box_revision'])
        self.assertNotEqual(result['assessments']['101']['classification'],'satisfied')

    def test_same_dispatch_stage_repeat_keeps_one_download_provenance(self):
        self.roster(2,'current')
        context=self.context('same-dispatch-repeat')
        self.roles.clients['research'].responses=[call('research_stage_jobs',stage_code='GH-1'),
            call('research_stage_jobs',stage_code='GH-1'),call('finish_role',status='researched',summary='same proof reused')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(len(result['candidate_refs']),1)
        self.assertEqual(len(result['verified_copilots']),1)
        self.assertEqual(self.downloads,['https://prts.maa.plus/copilot/get/101'])


if __name__ == '__main__':
    unittest.main()

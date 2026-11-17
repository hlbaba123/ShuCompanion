"""Offline activity scope/coverage proofs; invented stages and public sources."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta

from shu.core import ActionRejected, TaskEngine
from shu import leader_policy
from shu.leader_policy import research_satisfies_goal, research_stage_codes
from shu.maa_agents import MaaRoleAgents
from shu.workflow_store import WorkflowStore


class ActivityResearchScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)

    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def fixture(self, goal, *, codes=('GH-1', 'GH-2'), catalog_codes=None):
        contract = {'goal': goal, 'user_request': goal, 'task_spec': {'kind': 'lookup'}}
        current = self.store.submit(goal, contract)
        dispatch = self.store.ensure_dispatch(current['task_id'], 'research', 'source', {'contract': contract})
        task, epoch, identifier = current['task_id'], current['epoch'], dispatch['dispatch_id']
        receipt = {'status': 'researched', 'candidate_refs': [], 'evidence_refs': [],
                   'verified_copilots': [], 'candidate_pools': []}
        for index, code in enumerate(codes, 101):
            body = self.save(f'{code}-body.json', {'stage_name': 'stage_' + code, 'actions': [{'type': 'Deploy'}]})
            stage = {'task_id': task, 'epoch': epoch, 'goal': goal, 'query': code,
                     'source': 'https://prts.maa.plus/arknights/level', 'raw_sha256': 'b' * 64,
                     'stage_id': 'stage_' + code, 'level_id': 'level_' + code, 'code': code}
            stage['evidence_ref'] = self.save(f'{code}-stage.json', dict(stage))
            record = {'task_id': task, 'epoch': epoch, 'research_dispatch_id': identifier,
                      'copilot_id': str(index), 'path': body['path'], 'sha256': body['sha256'],
                      'stage_name': 'stage_' + code, 'title': 'offline', 'action_count': 1,
                      'source_url': f'https://prts.maa.plus/copilot/get/{index}', 'goal_stage': stage}
            candidate = self.save(f'{code}-candidate.json', record)
            proof = self.save(f'{code}-download-proof.json', {'task_id': task, 'epoch': epoch,
                'dispatch_id': identifier, 'read_only': True, 'tool': 'research_download',
                'result': {'ok': True, 'path': body['path'], 'sha256': body['sha256']}})
            receipt['candidate_refs'].append(candidate)
            receipt['evidence_refs'].extend([candidate, proof])
            facts = {key: record[key] for key in ('copilot_id', 'path', 'sha256', 'stage_name', 'title', 'source_url')}
            facts.update(code=code, mystery_code='prts://' + str(index), detail_url=f'https://prts.plus/?op={index}')
            receipt['verified_copilots'].append(facts)
        if catalog_codes:
            catalog = {'source': 'https://prts.maa.plus/arknights/level', 'raw_sha256': 'b' * 64,
                       'event_name': '虚构河岸', 'stages': [
                           {'code': code, 'stage_id': 'stage_' + code, 'level_id': 'level_' + code}
                           for code in catalog_codes]}
            ref = self.save('activity-catalog.json', catalog)
            receipt['activity_catalog'] = {'ok': True, 'event_name': '虚构河岸',
                'stages': catalog['stages'], 'has_more': False, 'stage_catalog_ref': ref}
            receipt['activity_scope'] = {'event_name': '虚构河岸', 'required_stage_codes': list(codes),
                'catalog_stage_codes': list(catalog_codes), 'excluded_stages': [],
                'catalog_ref': ref, 'coverage_verified': True, 'reason': 'offline'}
        return current, dispatch, receipt

    def complete(self, current, receipt):
        return self.store.complete_research_verified(current['task_id'], receipt['evidence_refs'], expected_epoch=current['epoch'])

    def pool(self, code, *, complete):
        coverage = {'complete': complete, 'collected': 1}
        ref = self.save(code + '-pool.json', {'pool_id': code, 'stage_id': 'stage_' + code,
            'coverage': coverage, 'candidates': [{'stage_name': 'stage_' + code}]})
        ref['pool_id'] = code
        return {'pool_ref': ref, 'coverage': coverage,
                'stage_resolution': {'levels': [{'code': code, 'stage_id': 'stage_' + code}]}}

    def test_all_explicit_codes_survive_single_stage_spec_and_shorter_model_goal(self):
        contract = {'goal': '查找GH-1作业JSON，本轮不匹配不导入',
                    'user_request': '查找GH-1和GH-2作业JSON，本轮不匹配不导入',
                    'task_spec': {'kind': 'lookup', 'stage_code': 'GH-1'}}
        self.assertEqual(research_stage_codes(contract), ['GH-1', 'GH-2'])
        _, _, receipt = self.fixture(contract['goal'], codes=('GH-1',))
        self.assertFalse(research_satisfies_goal(contract, receipt))

    def test_every_explicit_range_and_additional_code_is_required(self):
        contract = {'goal': '查找GH-1~GH-3以及XZ-EX-1作业JSON', 'task_spec': {'kind': 'lookup'}}
        self.assertEqual(research_stage_codes(contract), ['GH-1', 'GH-2', 'GH-3', 'XZ-EX-1'])

    def test_all_summaries_need_complete_pool_for_each_required_stage(self):
        goal = '查找GH-1~GH-2全部作业摘要，每关验证一份完整JSON，本轮不匹配不导入'
        current, dispatch, receipt = self.fixture(goal)
        receipt['candidate_pools'] = [self.pool('GH-1', complete=True), self.pool('GH-2', complete=False)]
        self.assertFalse(research_satisfies_goal(current['contract'], receipt))
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        with self.assertRaises(ActionRejected):
            self.complete(current, receipt)

    def test_complete_stage_pools_can_finish_multistage_summary_request(self):
        goal = '查找GH-1~GH-2全部作业摘要，每关验证一份完整JSON，本轮不匹配不导入'
        current, dispatch, receipt = self.fixture(goal)
        receipt['candidate_pools'] = [self.pool('GH-1', complete=True), self.pool('GH-2', complete=True)]
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        self.assertEqual(self.complete(current, receipt)['status'], 'completed')

    def test_activity_scope_cannot_silently_omit_second_catalog_stage(self):
        current, dispatch, receipt = self.fixture('查找当前活动作业JSON，本轮不匹配不导入',
                                                codes=('GH-1',), catalog_codes=('GH-1', 'GH-2'))
        self.assertFalse(research_satisfies_goal(current['contract'], receipt))
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        with self.assertRaises(ActionRejected):
            self.complete(current, receipt)

    def test_same_epoch_registered_download_can_complete_later_dispatch_without_new_io(self):
        current, first, receipt = self.fixture('查找GH-1作业JSON，本轮不匹配不导入', codes=('GH-1',))
        self.store.finish_dispatch(first['dispatch_id'], receipt)
        second = self.store.ensure_dispatch(current['task_id'], 'research', 'reuse', {'contract': current['contract']})
        self.store.finish_dispatch(second['dispatch_id'], copy.deepcopy(receipt))
        self.assertEqual(self.complete(current, receipt)['status'], 'completed')

    def scope_context(self):
        current, dispatch, receipt = self.fixture('查找当前活动作业JSON，本轮不匹配不导入',
            codes=('GH-1',), catalog_codes=('GH-1', 'GH-2'))
        roles = object.__new__(MaaRoleAgents)
        roles.directory, roles.store, roles._closed = self.root, self.store, False
        context = {key: current[key] for key in ('task_id', 'epoch', 'revision', 'contract')}
        context['dispatch_id'] = dispatch['dispatch_id']
        state = {'activity_catalog': receipt['activity_catalog'], 'research_refs': []}
        return roles, context, state

    def test_scope_tool_requires_each_omitted_code_and_actual_source(self):
        roles, context, state = self.scope_context()
        arguments = {'required_stage_codes': ['GH-1'], 'reason': '仅先看一关'}
        with self.assertRaises(ValueError):
            roles._execute_tool('research', 'research_scope', arguments, context, state, threading.Event())
        arguments['excluded_stages'] = [{'code': 'GH-2', 'reason': '尚未开放', 'source_url': 'https://example.org/unfetched'}]
        with self.assertRaises(ValueError):
            roles._execute_tool('research', 'research_scope', arguments, context, state, threading.Event())

    def test_scope_tool_binds_exclusion_to_real_public_research_reference(self):
        roles, context, state = self.scope_context()
        url = 'https://example.org/announcement'
        quote='GH-2将于2099年11月12日16:00开放。'
        proof = self.save('announcement.json', {'task_id': context['task_id'], 'epoch': context['epoch'],
            'dispatch_id': context['dispatch_id'], 'read_only': True, 'tool': 'research_fetch',
            'result': {'ok': True, 'url': url, 'data': {'text': quote}}})
        state['research_refs'].append(proof)
        result = roles._execute_tool('research', 'research_scope', {'required_stage_codes': ['GH-1'],
            'reason': '按公告目前开放范围', 'excluded_stages': [
                {'code': 'GH-2', 'reason_kind':'not_open_yet', 'reason': '公告将其列为后续开放',
                 'source_url': url, 'source_quote':quote, 'opens_at':'2099-11-12T16:00:00+08:00'}]}, context, state, threading.Event())
        self.assertTrue(result['coverage_verified'])
        self.assertEqual(result['catalog_stage_codes'], ['GH-1', 'GH-2'])
        self.assertEqual(result['excluded_stages'][0]['source_ref'], proof)

    def test_scope_tool_registers_every_catalog_code_without_needing_exclusions(self):
        roles, context, state = self.scope_context()
        result = roles._execute_tool('research', 'research_scope', {'required_stage_codes': ['GH-1', 'GH-2'],
            'reason': '覆盖目录所有关卡'}, context, state, threading.Event())
        self.assertTrue(result['coverage_verified'])
        self.assertEqual(result['excluded_stages'], [])

    def test_registered_announcement_allows_a_deliberate_future_stage_exclusion(self):
        current, dispatch, receipt = self.fixture('查找当前活动作业JSON，本轮不匹配不导入',
            codes=('GH-1',), catalog_codes=('GH-1', 'GH-2'))
        url = 'https://example.org/announcement'
        quote='GH-2将于2099年11月12日16:00开放。'
        proof = self.save('announcement-proof.json', {'task_id': current['task_id'], 'epoch': current['epoch'],
            'dispatch_id': dispatch['dispatch_id'], 'read_only': True, 'tool': 'research_fetch',
            'result': {'ok': True, 'url': url, 'data': {'text': quote}}})
        receipt['activity_scope']['excluded_stages'] = [
            {'code': 'GH-2', 'reason_kind':'not_open_yet', 'reason': '公告列为后续开放', 'source_url': url,
             'source_ref': proof, 'source_quote':quote, 'opens_at':'2099-11-12T16:00:00+08:00'}]
        receipt['evidence_refs'].append(proof)
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        self.assertEqual(self.complete(current, receipt)['status'], 'completed')

    def test_scope_exclusion_url_must_exist_in_the_registered_source_body(self):
        current, dispatch, receipt = self.fixture('查找当前活动作业JSON，本轮不匹配不导入',
            codes=('GH-1',), catalog_codes=('GH-1', 'GH-2'))
        proof = self.save('different-announcement.json', {'task_id': current['task_id'], 'epoch': current['epoch'],
            'dispatch_id': dispatch['dispatch_id'], 'read_only': True, 'tool': 'research_fetch',
            'result': {'ok': True, 'url': 'https://example.org/actually-fetched'}})
        receipt['activity_scope']['excluded_stages'] = [
            {'code': 'GH-2', 'reason_kind':'not_open_yet', 'reason': '声称依据',
             'source_url': 'https://example.org/never-fetched', 'source_ref': proof,
             'source_quote':'GH-2将于2099年11月12日16:00开放。', 'opens_at':'2099-11-12T16:00:00+08:00'}]
        receipt['evidence_refs'].append(proof)
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        with self.assertRaises(ActionRejected):
            self.complete(current, receipt)

    def test_complete_pool_for_another_stage_cannot_supply_missing_stage_coverage(self):
        current, dispatch, receipt = self.fixture('查找GH-1~GH-2全部作业摘要，每关验证一份完整JSON，本轮不匹配不导入')
        receipt['candidate_pools'] = [self.pool('GH-1', complete=True), self.pool('GH-2', complete=True)]
        # The metadata claims GH-2 but the actual pool is bound to a third stage.
        wrong = receipt['candidate_pools'][1]
        wrong['pool_ref'] = self.save('wrong-stage-pool.json', {'pool_id': 'wrong', 'stage_id': 'stage_OTHER-1',
            'coverage': wrong['coverage'], 'candidates': [{'stage_name': 'stage_OTHER-1'}]})
        wrong['pool_ref']['pool_id'] = 'wrong'
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        with self.assertRaises(ActionRejected):
            self.complete(current, receipt)

    def test_exploratory_pool_does_not_invalidate_complete_pools_for_each_required_stage(self):
        current, dispatch, receipt = self.fixture('查找GH-1~GH-2全部作业摘要，每关验证一份完整JSON，本轮不匹配不导入')
        receipt['candidate_pools'] = [self.pool('GH-1', complete=True), self.pool('GH-2', complete=True),
                                      self.pool('OTHER-1', complete=False)]
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        self.assertEqual(self.complete(current, receipt)['status'], 'completed')

    def test_nonordinary_exclusion_uses_exact_catalog_code_without_guessed_announcement(self):
        roles, context, state = self.scope_context()
        state['activity_catalog']['stages'].append({'code':'GH-TR-1','stage_id':'tutorial','level_id':'tutorial-level'})
        state['activity_catalog']['stage_catalog_ref']=self.save('activity-with-tutorial.json',{
            'source':'https://prts.maa.plus/arknights/level','event_name':'虚构河岸',
            'stages':state['activity_catalog']['stages']})
        result=roles._execute_tool('research','research_scope',{'required_stage_codes':['GH-1','GH-2'],
            'reason':'常规作战范围', 'excluded_stages':[{'code':'GH-TR-1','reason_kind':'not_ordinary','reason':'教程关代码'}]},
            context,state,threading.Event())
        excluded=result['excluded_stages'][0]
        self.assertEqual(excluded['source_ref'],state['activity_catalog']['stage_catalog_ref'])
        self.assertEqual(excluded['source_quote'],'GH-TR-1')
        self.assertEqual(excluded['stage_kind'],'tutorial')

    def test_ordinary_stage_cannot_be_relabelled_nonordinary_from_a_story_node(self):
        roles, context, state=self.scope_context()
        with self.assertRaises(ValueError):
            roles._execute_tool('research','research_scope',{'required_stage_codes':['GH-1'],
                'reason':'GH-ST-2之后才是下一部分', 'excluded_stages':[{
                    'code':'GH-2','reason_kind':'not_ordinary','reason':'剧情节点后的普通关猜成未开放'}]},
                context,state,threading.Event())

    def test_future_opening_exclusion_needs_exact_quote_target_and_date(self):
        check=getattr(leader_policy,'opening_exclusion_supported',None)
        self.assertIsNotNone(check,'opening exclusion must inspect actual source text')
        now=datetime(2030,11,7,18,tzinfo=timezone(timedelta(hours=8)))
        url='https://example.org/announcement'
        quote='普通关卡GH-1~GH-10将于2030年11月12日16:00开放。'
        proof={'tool':'research_fetch','result':{'ok':True,'url':url,'data':{'content':quote}}}
        item={'code':'GH-5','reason_kind':'not_open_yet','source_url':url,
              'source_quote':quote,'opens_at':'2030-11-12T16:00:00+08:00'}
        self.assertTrue(check(item,proof,now=now))
        for change in ({'source_quote':'GH-5未开放，这句并不存在。'},
                       {'opens_at':'2030-11-13T16:00:00+08:00'},
                       {'source_url':'https://example.org/other'}):
            with self.subTest(change=change):self.assertFalse(check({**item,**change},proof,now=now))
        story='GH-ST-2剧情节点：2030年11月12日16:00开启新章节。'
        storyproof={**proof,'result':{'ok':True,'url':url,'data':{'content':story}}}
        self.assertFalse(check({**item,'source_quote':story},storyproof,now=now))
        past='普通关卡GH-1~GH-10于2030年11月1日16:00开放。'
        pastproof={**proof,'result':{'ok':True,'url':url,'data':{'content':past}}}
        self.assertFalse(check({**item,'source_quote':past,'opens_at':'2030-11-01T16:00:00+08:00'},pastproof,now=now))


if __name__ == '__main__':
    unittest.main()

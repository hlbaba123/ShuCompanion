"""Offline role permissions and real SQLite effect admission; no model/GUI/network."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from shu.core import TaskEngine
from shu.providers import ProviderConfig
from shu.maa_uia import MAA_EXECUTABLE
from shu.workflow_store import WorkflowStore
from shu import maa_agents


def call(name, **arguments):
    return {'role': 'assistant', 'content': None, 'reasoning_content': 'PRIVATE REASONING',
            'tool_calls': [{'id': 'call-' + name, 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


def element(identifier, name, patterns=(), **extra):
    return {'id': identifier, 'name': name, 'automation_id': identifier,
            'control_type': 'Text', 'patterns': list(patterns), 'enabled': True,
            'offscreen': False, 'password': False, 'read_only': not patterns,
            'value': '', **extra}


def snapshot(number, *elements):
    return {'snapshot_id': str(number), 'timestamp': time.time(), 'snapshot_status': 'ok',
            'window': {'hwnd': 10, 'pid': 20, 'executable': MAA_EXECUTABLE},
            'elements': list(elements)}


class FakeClient:
    def __init__(self, config):
        self.config, self.responses, self.requests = config, [], []

    def complete(self, messages, **kwargs):
        if self.config.thinking and kwargs.get('tool_choice') == 'required':
            raise ValueError('Existing DeepSeek transport rejects required in thinking mode')
        self.requests.append(copy.deepcopy(messages))
        return self.responses.pop(0)


class FakeResearch:
    def __init__(self, config, cache_dir, **kwargs):
        self.cache_dir = Path(cache_dir)
        self.discovered_copilot_urls = set()
        self.catalog_calls = []

    def levels(self, query):
        return {'ok': True, 'tool': 'levels', 'query': query,
                'data': {'source': 'https://prts.maa.plus/arknights/level', 'raw_sha256': '1' * 64,
                         'levels': [{'stage_id': 'level-test-1', 'level_id': 'level/test/1',
                                     'code': 'TEST-1', 'match': 'exact_code_or_id'}]}}

    def catalog(self, stage_or_keyword):
        self.catalog_calls.append(stage_or_keyword)
        source = 'https://prts.maa.plus/copilot/get/123'
        self.discovered_copilot_urls.add(source)
        return {'ok': True, 'source': source}

    def download_copilot(self, url):
        body = {'stage_name': 'level-test-1', 'doc': {'title': '真实关卡作业'},
                'actions': [{'type': 'Deploy'}]}
        raw = json.dumps(body, ensure_ascii=False).encode()
        path = self.cache_dir / 'copilots' / '123.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {'ok': True, 'tool': 'download_copilot', 'source_url': url,
                'sha256': hashlib.sha256(raw).hexdigest(), 'path': str(path)}


class FakeWindows:
    def __init__(self, directory):
        pass

    def list_windows(self, game_only=True):
        if game_only:
            raise AssertionError('MAA enumeration must request all windows')
        return [{'hwnd': 10, 'pid': 20, 'executable': MAA_EXECUTABLE}]


class FakeSupervisor:
    def __init__(self, identity, **kwargs):
        self.target_identity, self.snapshots, self.commands = identity, [], []
        self.before_admission = None
        self.revoked = self.unknown = False
        self.reconciliations = []

    def request(self, command, cancel_event=None, admit_callback=None, **kwargs):
        self.commands.append(command)
        if command['op'] == 'snapshot':
            return {'status': 'completed', 'submission_entered': False,
                    'result': self.snapshots.pop(0)}
        if self.revoked or self.unknown:
            return {'status': 'rejected', 'submission_entered': False}
        if self.before_admission:
            self.before_admission()
        try:
            admit_callback()
        except Exception:
            return {'status': 'not_issued', 'submission_entered': False}
        return {'status': 'completed', 'submission_entered': True,
                'result': {'ok': True, 'result': 'dispatched', 'after': None}}

    def request_stop(self, command, admit_callback=None, **kwargs):
        self.revoked = True
        admit_callback()
        return {'status': 'stopping', 'submission_entered': True}

    def health(self):
        return {'revoked': self.revoked, 'unknown': self.unknown}

    def reconcile(self, evidence, *, resume=False):
        self.reconciliations.append(evidence)
        self.unknown = False
        if resume:
            self.revoked = False

    def close(self):
        pass


class MaaAgentTests(unittest.TestCase):
    def test_import_diagnosis_context_keeps_catalog_reference_without_raw_catalog(self):
        from shu.maa_agents import _model_context
        reference = {'path': 'catalog-proof.json', 'sha256': 'a' * 64}
        context = {'contract': {'current_deliverable': 'maa_import'}, 'role_results': {
            'catalog': {'status': 'completed', 'structure_ref': reference,
                        'activity_catalog': {'stages': [{'raw': 'x' * 3000} for _ in range(80)]}}}}
        projected = _model_context(context)
        self.assertEqual(projected['role_results']['catalog']['structure_ref'], reference)
        self.assertLess(len(json.dumps(projected)), 5000)
        self.assertEqual(len(context['role_results']['catalog']['activity_catalog']['stages']), 80)

    def test_verified_catalog_hands_off_without_more_web_search(self):
        context = self.context('catalog')
        context['contract']['current_deliverable'] = 'copilot_codes'
        client = self.roles.clients['catalog']
        client.responses = [call('normalize_structure', proposal={}),
                            call('research_search', query='多余的作业网页搜索')]
        ref = {'path': 'structure', 'sha256': 'digest'}

        def normalize(role, name, arguments, active, state, cancel):
            self.assertEqual(name, 'normalize_structure')
            state['structure_ref'] = ref
            return {'ok': True, 'activity_name': '示例', 'stage_count': 2}

        with patch.object(self.roles, '_execute_tool', side_effect=normalize), \
             patch('shu.planned_agents.verify_planned_completion', return_value={'complete': True}):
            result = self.roles.handlers['catalog'](context, self.cancel)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['structure_ref'], ref)
        self.assertEqual(len(client.requests), 1)

    def test_resolved_readonly_gap_does_not_start_another_model_round(self):
        context = self.context('leader')
        context['contract']['current_deliverable'] = 'copilot_codes'
        context['role_results']['research'] = {'closed_not_retryable': True}
        precheck = {'closed': True, 'complete': False, 'summary': '当前可查询 0/1 关有主选',
                    'items': [{'stage_code': 'TEST-1', 'status': 'search_incomplete',
                               'attempt': 2, 'reason': 'catalog_unverified'}]}
        with patch.object(maa_agents, '_planned_precheck', return_value=precheck):
            result = self.roles.handlers['leader'](context, self.cancel)
        self.assertEqual(result['next_role'], 'ask')
        self.assertIn('部分记录未核实', result['summary'])
        self.assertEqual(self.roles.clients['leader'].requests, [])

        precheck['items'][0]['reason'] = 'network_error'
        self.roles.clients['leader'].responses = [call('delegate', next_role='diagnosis', task='核对连接', summary='查询连接失败')]
        with patch.object(maa_agents, '_planned_precheck', return_value=precheck):
            result = self.roles.handlers['leader'](context, self.cancel)
        self.assertEqual(result['next_role'], 'diagnosis')

    def test_activity_cannot_finish_successfully_without_registered_scope(self):
        self.current=self.store.submit('查询新活动的作业JSON',{'goal':'查询新活动的作业JSON',
            'task_spec':{'kind':'lookup'}})
        context=self.context('research')
        self.roles.clients['research'].responses=[call('research_levels',query='TEST-1'),
            call('research_catalog',stage_or_keyword='level-test-1'),
            call('research_download',url='https://prts.maa.plus/copilot/get/123'),
            call('finish_role',status='researched',summary='活动已完成'),
            call('finish_role',status='unknown',summary='仍需登记范围')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(result['status'],'unknown')
        self.assertTrue(result['candidate_refs'])
        replies=[json.loads(m['content']) for m in self.roles.clients['research'].requests[-1] if m['role']=='tool']
        self.assertTrue(any(r.get('status')=='rejected' and 'activity_scope' in str(r.get('summary','')) for r in replies))

    def test_activity_catalog_bounds_lookup_before_goal_scope_is_committed(self):
        self.current=self.store.submit('查询新活动的作业JSON',{'goal':'查询新活动的作业JSON',
            'task_spec':{'kind':'lookup'}})
        context=self.context('research');scopes=[]
        tool=FakeResearch(ProviderConfig(),self.directory/'public')
        tool.set_level_scope=lambda codes:scopes.append(codes)
        state={'research':tool,'activity_catalog':{'ok':True,'stages':[{'code':f'TEST-{i}'} for i in range(1,11)]}}
        self.roles._execute_tool('research','research_levels',{'query':'TEST-1'},context,state,self.cancel)
        self.assertEqual(scopes,[[f'TEST-{i}' for i in range(1,11)]])

    def test_truncated_thinking_response_gets_one_bounded_budget_retry(self):
        context=self.context('research')
        class LengthClient(FakeClient):
            def complete(self,messages,**kwargs):
                result=super().complete(messages,**kwargs)
                self.requests[-1].append({'role':'system','content':'budget='+str(self.config.max_tokens)})
                self.last_finish_reason='length' if len(self.requests)==1 else 'tool_calls'
                return result
        client=LengthClient(ProviderConfig(thinking=True,max_tokens=4096))
        client.responses=[{'role':'assistant','content':None,'reasoning_content':'partial-fixture'},
            call('finish_role',status='unknown',summary='预算重试后提交，未知信息仍未知')]
        self.roles.clients['research']=client
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(result['summary'],'预算重试后提交，未知信息仍未知')
        self.assertEqual(len(client.requests),2)
        self.assertEqual(client.requests[1][-1]['content'],'budget=8192')
        self.assertEqual(client.config.max_tokens,4096)
    def test_malformed_tool_arguments_are_repaired_without_losing_real_research(self):
        context=self.context('research')
        broken=call('finish_role',status='researched',summary='not valid')
        broken['tool_calls'][0]['function']['arguments']='{"status":'
        self.roles.clients['research'].responses=[call('research_catalog',stage_or_keyword='TEST-1'),
            broken,call('finish_role',status='researched',summary='已修复格式，公开资料有实际来源')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(result['status'],'researched')
        self.assertTrue(result['evidence_refs'])
        self.assertEqual(len(self.roles.clients['research'].requests),3)

    def test_repeated_empty_knowledge_calls_stop_before_spending_all_role_rounds(self):
        context=self.context('leader')
        self.roles.clients['leader'].responses=[call('knowledge',query='同一项未收录资料') for _ in range(8)]
        result=self.roles.handlers['leader'](context,self.cancel)
        self.assertEqual(result['code'],'repeated_tool_no_progress')
        self.assertLessEqual(len(self.roles.clients['leader'].requests),3)
    def test_lookup_contract_cannot_import_or_toggle_maa_controls(self):
        context=self.context('maa')
        context['contract']={**context['contract'],'task_spec':{'kind':'lookup'}}
        with self.assertRaisesRegex(Exception,'只读'):
            self.roles._act(context,{}, {'element_id':'any','action':'toggle','value':None},self.cancel)

    def test_read_only_public_research_keeps_tool_evidence_without_copilot_download(self):
        context=self.context('research')
        self.roles.clients['research'].responses=[call('research_catalog',stage_or_keyword='TEST-1'),
            call('finish_role',status='researched',summary='公开目录已读取，未导入MAA')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertTrue(result['evidence_refs'])
        self.assertEqual(result['candidate_refs'],[])
        proof=self.roles._read_ref(result['evidence_refs'][0])
        self.assertEqual(proof['tool'],'research_catalog')
        self.assertEqual(proof['task_id'],context['task_id'])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.engine = TaskEngine(self.directory)
        self.store = WorkflowStore(self.engine)
        self.current = self.store.submit('导入模型自主找到的 TEST-1 作业', {'allow_start': False, 'goal': '导入 TEST-1'})
        self.cancel = threading.Event()
        self.patches = [patch.object(maa_agents, 'DeepSeekClient', FakeClient),
                        patch.object(maa_agents, 'ResearchTools', FakeResearch),
                        patch.object(maa_agents, 'WindowController', FakeWindows),
                        patch.object(maa_agents, 'GuiSupervisor', FakeSupervisor),
                        patch.object(maa_agents, '_observed_identity', lambda window: {**window, 'creation_time': 30})]
        for item in self.patches:
            item.start()
        self.roles = maa_agents.MaaRoleAgents(self.store, self.directory / 'roles',
                                             ProviderConfig(), ProviderConfig(), None,
                                             lambda: True, lambda: True)

    def tearDown(self):
        self.roles.close()
        for item in reversed(self.patches):
            item.stop()
        self.engine.close()
        self.temp.cleanup()

    def context(self, role, results=None):
        current = self.store.current(self.current['task_id'])
        dispatch = self.store.ensure_dispatch(current['task_id'], role,
                                              role + ':' + str(len(results or {})),
                                              {'task': '自主导入', 'contract': current['contract']})
        return {**{key: current[key] for key in ('task_id', 'epoch', 'revision', 'contract')},
                'dispatch_id': dispatch['dispatch_id'], 'task': '自主导入',
                'role_results': results or {}, 'last_role': ''}

    def test_closed_plan_uses_verified_receipts_for_leader_decision(self):
        from unittest.mock import Mock
        for complete, expected in ((True, 'complete'), (False, 'ask')):
            with self.subTest(complete=complete):
                context = self.context('leader')
                context['contract'] = {**context['contract'], 'current_deliverable':'copilot_codes'}
                self.roles.clients['leader'].responses = [call('delegate', next_role='research', task='继续', summary='继续')]
                request_count = len(self.roles.clients['leader'].requests)
                plan = Mock(); plan.current.return_value = {'steps':[{'status':'two_verified' if complete else 'search_incomplete'}]}
                with patch('shu.leader_plan.PlanStore', return_value=plan), patch.object(maa_agents, '_planned_precheck', return_value={'complete':complete,'summary':'真实凭据摘要'}):
                    result = self.roles.handlers['leader'](context, self.cancel)
                self.assertEqual(result['next_role'], expected)
                self.assertEqual(result['summary'], '真实凭据摘要')
                if complete:
                    # Verified receipts finish directly; an extra model round cannot
                    # improve the authority of this already persisted evidence.
                    self.assertEqual(len(self.roles.clients['leader'].requests), request_count)
                else:
                    self.assertGreater(len(self.roles.clients['leader'].requests), request_count)
                    shown = json.loads(self.roles.clients['leader'].requests[-1][1]['content'])
                    self.assertFalse(shown['planned_delivery_precheck']['complete'])

    def test_leader_explicit_question_kind_survives_delegate(self):
        for kind in ('business', 'blocked'):
            with self.subTest(kind=kind):
                self.roles.clients['leader'].responses = [call('delegate', next_role='ask',
                    task='', summary='请确认原版还是复刻目录？', ask_kind=kind)]
                result = self.roles.handlers['leader'](self.context('leader'), self.cancel)
                self.assertEqual(result.get('ask_kind'), kind)

    def test_leader_unspecified_question_kind_defaults_to_blocked(self):
        self.roles.clients['leader'].responses = [call('delegate', next_role='ask',
            task='', summary='缺少真实目录来源，不能继续。')]
        result = self.roles.handlers['leader'](self.context('leader'), self.cancel)
        self.assertEqual(result.get('ask_kind'), 'blocked')

    def research(self):
        context = self.context('research')
        self.roles.clients['research'].responses = [call('research_levels', query='TEST-1'),
            call('research_catalog', stage_or_keyword='level-test-1'),
            call('research_download', url='https://prts.maa.plus/copilot/get/123'),
            call('finish_role', summary='候选已完整下载', status='researched')]
        result = self.roles.handlers['research'](context, self.cancel)
        self.store.finish_dispatch(context['dispatch_id'], result)
        return result

    def bind(self, snapshots):
        self.roles._bind_supervisor()
        self.roles.supervisor.snapshots = snapshots

    def test_five_clients_and_disjoint_capabilities_no_game_input(self):
        self.assertEqual(len({id(v) for v in self.roles.clients.values()}), 5)
        self.assertIsNot(self.roles.clients['maa'].config, self.roles.clients['research'].config)
        tools = self.roles.tool_registries
        self.assertIn('delegate', tools['leader'])
        self.assertIn('research_download', tools['research'])
        self.assertIn('maa_act', tools['maa'])
        self.assertNotIn('maa_act', tools['diagnosis'])
        self.assertFalse(any('game' in name or 'click' in name for registry in tools.values() for name in registry))

    def test_leader_gets_actual_role_capabilities_without_a_fixed_route(self):
        context = self.context('leader')
        self.roles.clients['leader'].responses = [call('delegate', next_role='research',
            task='确认本次游戏内活动', summary='先补足活动信息')]
        self.roles.handlers['leader'](context, self.cancel)
        request = self.roles.clients['leader'].requests[0]
        system = request[0]['content']
        payload = json.loads(request[1]['content'])
        self.assertNotIn('先检查 MAA，再自主研究', system)
        self.assertIn('available_capabilities', payload)
        self.assertTrue(payload['available_capabilities']['planning_without_input'])
        self.assertFalse(payload['available_capabilities']['battle_start'])
        self.assertIn('inspect_maa', payload['available_capabilities']['roles']['maa']['tools'])
        self.assertNotIn('maa_act', payload['available_capabilities']['roles']['diagnosis']['tools'])

    def test_factual_read_only_inspection_is_not_downgraded_to_unknown(self):
        context = self.context('maa')
        self.bind([snapshot(1,element('status','MAA 当前空闲'))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('finish_role',status='observed',summary='已读取 MAA 空闲状态，未输入')]
        result = self.roles.handlers['maa'](context,self.cancel)
        self.assertEqual(result['status'],'observed')
        self.assertTrue(result['evidence_refs'])
        self.assertFalse(result['battle_start_submitted'])

    def test_unsupported_claim_of_inspection_stays_unknown_without_real_evidence(self):
        context = self.context('maa')
        self.roles.clients['maa'].responses = [call('finish_role',status='observed',summary='相信我看到了')]
        result = self.roles.handlers['maa'](context,self.cancel)
        self.assertEqual(result['status'],'unknown')

    def test_activity_reports_tool_boundaries_without_model_or_tool_payloads(self):
        events = []
        self.roles.activity_callback = lambda kind, details: events.append((kind, details))
        result = self.research()
        self.assertEqual(result['status'], 'researched')
        self.assertEqual([kind for kind, data in events], ['tool_started', 'tool_finished'] * 4)
        self.assertEqual([data['tool'] for kind, data in events if kind == 'tool_started'],
                         ['research_levels', 'research_catalog', 'research_download', 'finish_role'])
        for kind, data in events:
            self.assertEqual(data['role'], 'research')
            self.assertEqual(data['task_id'], self.current['task_id'])
            self.assertTrue(data['dispatch_id'])
            self.assertNotIn('arguments', data)
            self.assertNotIn('result', data)
        self.assertNotIn('PRIVATE REASONING', json.dumps(events))

    def test_tool_activity_listener_failure_cannot_change_execution(self):
        def broken(kind, details):
            raise RuntimeError('a disconnected UI listener')
        self.roles.activity_callback = broken
        self.assertEqual(self.research()['status'], 'researched')

    def test_tool_failure_is_visible_and_cancel_drops_late_completion(self):
        events = []
        def progress(kind, details):
            events.append((kind, details))
            if kind == 'tool_started':
                self.cancel.set()
        self.roles.activity_callback = progress
        self.roles.clients['research'].responses = [call('knowledge', query='测试')]
        result = self.roles.handlers['research'](self.context('research'), self.cancel)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual([kind for kind, data in events], ['tool_started'])

    def test_failed_tool_reports_failure_without_private_exception_text(self):
        events = []
        self.roles.activity_callback = lambda kind, details: events.append((kind, details))
        self.roles.clients['research'].responses = [call('knowledge', query='测试')]
        with patch.object(self.roles, '_execute_tool', side_effect=RuntimeError('Bearer private-exception')):
            result = self.roles.handlers['research'](self.context('research'), self.cancel)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual([kind for kind, data in events], ['tool_started', 'tool_finished'])
        self.assertEqual(events[-1][1]['status'], 'failed')
        self.assertNotIn('private-exception', json.dumps(events))

    def test_roster_unknown_is_available_to_research_without_game_or_gui(self):
        self.assertIn('get_roster', self.roles.tool_registries['research'])
        self.roles.clients['research'].responses = [call('get_roster'),
            call('finish_role', status='unknown', summary='缺少实际干员档案，不能判断作业可用')]
        result = self.roles.handlers['research'](self.context('research'), self.cancel)
        self.assertEqual(result['status'], 'unknown')
        feedback = [json.loads(m['content']) for m in self.roles.clients['research'].requests[-1]
                    if m['role'] == 'tool']
        self.assertEqual(feedback[0]['status'], 'unknown')
        self.assertIsNone(self.roles.supervisor)

    def test_roster_export_import_is_fixed_local_file_and_does_not_grant_account_truth(self):
        self.assertIn('import_roster_export', self.roles.tool_registries['maa'])
        path = self.roles.directory / 'roster-imports' / 'maa-roster.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'done': True,
            'all_opers': [{'id': 'fixture', 'name': 'Fixture', 'own': True, 'rarity': 6}],
            'own_opers': [{'id': 'fixture', 'name': 'Fixture', 'own': True, 'elite': 2,
                           'level': 80, 'potential': 1, 'rarity': 6}]}))
        self.roles.clients['maa'].responses = [call('import_roster_export'),
            call('finish_role', status='completed', summary='本地导出已入档')]
        self.roles.handlers['maa'](self.context('maa'), self.cancel)
        from shu.maa_roster_tools import RosterTools
        box = RosterTools(self.store, self.roles.directory).current('local-default')
        self.assertTrue(box['complete'])
        self.assertIsNone(self.roles.supervisor)

    def test_leader_delegates_and_does_not_persist_raw_response(self):
        self.roles.clients['leader'].responses = [call('delegate', next_role='research',
                                                       task='查找真实作业', summary='需要查资料')]
        result = self.roles.handlers['leader'](self.context('leader'), self.cancel)
        self.assertEqual(result['next_role'], 'research')
        self.assertNotIn('PRIVATE REASONING', json.dumps(result))
        self.assertFalse(list(self.directory.rglob('*.json')))

    def test_leader_can_explicitly_request_registered_research_adoption(self):
        self.roles.clients['leader'].responses = [call('delegate', next_role='adopt_research',
            source_dispatch_id='historical-dispatch', task='原研究问题', summary='核对旧研究后采用')]
        result = self.roles.handlers['leader'](self.context('leader'), self.cancel)
        self.assertEqual(result['next_role'], 'adopt_research')
        self.assertEqual(result['source_dispatch_id'], 'historical-dispatch')

    def test_existing_thinking_configuration_can_use_tool_loop(self):
        self.roles.clients['leader'].config.thinking = True
        self.roles.clients['leader'].responses = [call('delegate', next_role='research',
                                                       task='查找真实作业', summary='需要查资料')]
        result = self.roles.handlers['leader'](self.context('leader'), self.cancel)
        self.assertEqual(result['next_role'], 'research')

    def test_research_download_refs_survive_new_role_instance_and_hash_change_rejected(self):
        result = self.research()
        self.assertTrue(result['candidate_refs'])
        candidate = self.roles._candidates(self.context('maa', {'research': result}))[0]
        self.assertEqual(candidate['copilot_id'], '123')
        self.roles.close()
        self.roles = maa_agents.MaaRoleAgents(self.store, self.directory / 'roles',
                                             ProviderConfig(), ProviderConfig(), None,
                                             lambda: True, lambda: True)
        self.assertEqual(self.roles._candidates(self.context('maa', {'research': result}))[0], candidate)
        Path(candidate['path']).write_text('{}')
        self.assertEqual(self.roles._candidates(self.context('maa', {'research': result})), [])

    def test_start_target_denied_even_when_model_labels_it_import(self):
        self.bind([snapshot(1, element('import', '开始', ['invoke']))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='import', action='invoke', value=None),
            call('finish_role', summary='无法导入', status='unknown')]
        result = self.roles.handlers['maa'](self.context('maa'), self.cancel)
        self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])
        self.assertFalse(result['battle_start_submitted'])

    def test_arbitrary_text_denied_and_no_generic_keyboard_tool(self):
        self.bind([snapshot(1, element('box', '作业码', ['set_value']))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='999'),
            call('finish_role', summary='没有真实下载', status='unknown')]
        self.roles.handlers['maa'](self.context('maa'), self.cancel)
        self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])

    def test_editable_echo_cannot_claim_import_success(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])),
                   snapshot(2, element('box', '真实关卡作业', ['set_value'], value='123'))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='导入成功', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(actual['status'], 'import_verified')

    def test_real_download_input_and_independent_loaded_text_verify_import(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])),
                   snapshot(2, element('loaded', '已加载 真实关卡作业'))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='已核验', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertEqual(actual['status'], 'import_verified')
        self.assertTrue(actual['evidence_refs'])
        self.assertFalse(actual['battle_start_submitted'])
        self.assertEqual(len([c for c in self.roles.supervisor.commands if c['op'] == 'act']), 1)
        operations = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations').fetchall()
        self.assertEqual(operations[0]['status'], 'verified')
        self.assertIsNotNone(operations[0]['submitted_at'])

    def test_title_substring_in_another_stage_message_cannot_verify_import(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])),
                   snapshot(2, element('loaded', '其他关卡：真实关卡作业'))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='尚待核实', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(actual['status'], 'import_verified')

    def test_exact_loaded_title_cannot_override_conflicting_explicit_stage(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value']), element('stage', '关卡：TEST-2')),
                   snapshot(2, element('loaded', '真实关卡作业'), element('stage', '关卡：TEST-2'))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='尚待核实', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(actual['status'], 'import_verified')

    def test_exact_title_with_unrelated_guide_text_can_verify_import(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])),
                   snapshot(2, element('loaded', '真实关卡作业'),
                            element('guide', '其他关卡攻略请参阅使用说明'))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='已核验', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertEqual(actual['status'], 'import_verified')

    def test_appending_unrelated_log_line_does_not_refresh_old_loaded_title(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value']), element('log', '真实关卡作业')),
                   snapshot(2, element('log', '真实关卡作业\n收到其他消息'))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='尚待核实', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(actual['status'], 'import_verified')

    def test_truncated_snapshot_cannot_authorize_import_write(self):
        result = self.research()
        truncated = snapshot(1, element('box', '作业码', ['set_value']))
        truncated['possibly_truncated'] = True
        self.bind([truncated])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('finish_role', summary='控件不完整', status='unknown')]
        self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])

    def test_truncated_loaded_snapshot_cannot_verify_import(self):
        result = self.research()
        truncated = snapshot(2, element('loaded', '真实关卡作业'))
        truncated['possibly_truncated'] = True
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])), truncated])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='控件不完整', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(actual['status'], 'import_verified')

    def test_stop_between_issue_and_supervisor_submission_blocks_gui(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value']))])
        self.roles.supervisor.before_admission = lambda: self.store.control(self.current['task_id'], 'stop')
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('finish_role', summary='已停止', status='unknown')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        row = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations').fetchone()
        self.assertEqual(row['status'], 'not_issued')
        self.assertIsNone(row['submitted_at'])
        self.assertNotEqual(actual['status'], 'import_verified')

    def test_diagnosis_cannot_execute_gui_or_grant_permissions(self):
        self.roles.clients['diagnosis'].responses = [call('maa_act', element_id='x', action='invoke'),
            call('finish_role', status='diagnosed', summary='需要重新读取 MAA', proposals=['先读 GUI'])]
        result = self.roles.handlers['diagnosis'](self.context('diagnosis'), self.cancel)
        self.assertIsNone(self.roles.supervisor)
        self.assertEqual(result['proposals'], ['先读 GUI'])

    def test_invalid_batch_ids_cannot_execute_first_tool(self):
        response = call('inspect_maa')
        response['tool_calls'].append(copy.deepcopy(response['tool_calls'][0]))
        self.roles.clients['maa'].responses = [copy.deepcopy(response) for _ in range(3)]
        result = self.roles.handlers['maa'](self.context('maa'), self.cancel)
        self.assertEqual(result['code'], 'model_protocol_invalid')
        self.assertIsNone(self.roles.supervisor)

    def test_unknown_control_is_not_a_semantic_import_action(self):
        self.bind([snapshot(1, element('button-17', '', ['invoke']))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='button-17', action='invoke', value=None),
            call('finish_role', summary='未知控件', status='unknown')]
        self.roles.handlers['maa'](self.context('maa'), self.cancel)
        self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])

    def test_bare_confirm_and_file_dialog_controls_cannot_commit_unrelated_effects(self):
        for name in ('确定', '确认', '打开', '浏览', '取消', '关闭', '读取账号'):
            with self.subTest(name=name):
                self.bind([snapshot(1, element('generic', name, ['invoke']))])
                self.roles.clients['maa'].responses = [call('inspect_maa'),
                    call('maa_act', element_id='generic', action='invoke', value=None),
                    call('finish_role', summary='不属于导入', status='unknown')]
                self.roles.handlers['maa'](self.context('maa'), self.cancel)
                self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])

    def test_unnamed_wpf_part_requires_selected_copilot_page_and_unique_editable_part(self):
        result = self.research()
        part = element('box', '', ['set_value'], automation_id='PART_TextBox')
        page = element('page', 'MaaWpfGui.ViewModels.CopilotViewModel', ['select'], selected=True)
        for elements, accepted in (([part], False), ([page, part], True),
                                   ([page, part, {**part, 'id': 'other'}], False),
                                   ([{**page, 'selected': False}, part], False)):
            with self.subTest(accepted=accepted, count=len(elements)):
                self.bind([snapshot(1, *elements)])
                self.roles.supervisor.commands = []
                self.roles.clients['maa'].responses = [call('inspect_maa'),
                    call('maa_act', element_id='box', action='set_value', value='123'),
                    call('finish_role', summary='仅尝试导入', status='unknown')]
                self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
                self.assertEqual(bool([c for c in self.roles.supervisor.commands if c['op'] == 'act']), accepted)

    def test_import_button_requires_copilot_page_and_this_dispatch_verified_source_input(self):
        result = self.research()
        page = element('page', 'MaaWpfGui.ViewModels.CopilotViewModel', ['select'], selected=True)
        button = element('import', '导入', ['invoke'])
        self.bind([snapshot(1, page, button, element('box', '作业码', ['set_value'])),
                   snapshot(2, page, button)])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='import', action='invoke', value=None),
            call('maa_act', element_id='box', action='set_value', value='123'), call('inspect_maa'),
            call('maa_act', element_id='import', action='invoke', value=None),
            call('finish_role', status='unknown', summary='仍需读回')]
        self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        effects = [c['args'] for c in self.roles.supervisor.commands if c['op'] == 'act']
        self.assertEqual([e['action'] for e in effects], ['set_value', 'invoke'])

    def test_only_copilot_navigation_can_be_selected_without_import_input(self):
        page = element('page', 'MaaWpfGui.ViewModels.CopilotViewModel', ['select'], selected=False)
        self.bind([snapshot(1, page)])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='page', action='select', value=None),
            call('finish_role', status='unknown', summary='已选择作业页，仍需导入')]
        self.roles.handlers['maa'](self.context('maa'), self.cancel)
        self.assertEqual([c['args']['action'] for c in self.roles.supervisor.commands if c['op'] == 'act'], ['select'])

    def test_download_without_resolved_goal_stage_cannot_become_import_candidate(self):
        context = self.context('research')
        self.roles.clients['research'].responses = [call('research_catalog', stage_or_keyword='TEST-1'),
            call('research_download', url='https://prts.maa.plus/copilot/get/123'),
            call('finish_role', summary='未解析关卡', status='researched')]
        result = self.roles.handlers['research'](context, self.cancel)
        self.assertEqual(result['candidate_refs'], [])

    def test_download_for_other_real_stage_cannot_satisfy_goal_even_with_matching_title(self):
        original = FakeResearch.levels

        def wrong_level(research, query):
            result = original(research, query)
            result['data']['levels'][0].update(stage_id='level-test-2', code='TEST-2')
            return result

        with patch.object(FakeResearch, 'levels', wrong_level):
            context = self.context('research')
            self.roles.clients['research'].responses = [call('research_levels', query='TEST-2'),
                call('research_catalog', stage_or_keyword='TEST-2'),
                call('research_download', url='https://prts.maa.plus/copilot/get/123'),
                call('finish_role', summary='其他关卡', status='researched')]
            result = self.roles.handlers['research'](context, self.cancel)
        self.assertEqual(result['candidate_refs'], [])

    def test_download_json_stage_must_match_real_goal_lookup(self):
        original = FakeResearch.download_copilot

        def wrong_copilot(research, url):
            result = original(research, url)
            path = Path(result['path'])
            body = json.loads(path.read_text(encoding='utf-8'))
            body['stage_name'] = 'level-other-2'
            path.write_text(json.dumps(body), encoding='utf-8')
            result['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            return result

        with patch.object(FakeResearch, 'download_copilot', wrong_copilot):
            result = self.research()
        self.assertEqual(result['candidate_refs'], [])

    def test_research_success_label_is_canonicalized_without_losing_download_proof(self):
        context=self.context('research')
        self.roles.clients['research'].responses=[call('research_levels',query='TEST-1'),
            call('research_catalog',stage_or_keyword='TEST-1'),
            call('research_download',url='https://prts.maa.plus/copilot/get/123'),
            call('finish_role',status='success',summary='已取得完整作业')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(result['status'],'researched')
        self.assertTrue(result['candidate_refs'])
        self.assertTrue(result['verified_copilots'])

    def test_research_status_schema_is_finite_and_ok_legacy_alias_keeps_real_proof(self):
        registry=self.roles.tool_registries['research']['finish_role']['function']['parameters']
        self.assertIn('researched',registry['properties']['status']['enum'])
        self.assertNotIn('ok',registry['properties']['status']['enum'])
        context=self.context('research')
        self.roles.clients['research'].responses=[call('research_levels',query='TEST-1'),
            call('research_catalog',stage_or_keyword='TEST-1'),
            call('research_download',url='https://prts.maa.plus/copilot/get/123'),
            call('finish_role',status='ok',summary='已取得完整作业')]
        result=self.roles.handlers['research'](context,self.cancel)
        self.assertEqual(result['status'],'researched');self.assertTrue(result['candidate_refs'])

    def test_historical_download_requires_current_explicit_adoption_and_rechecks_nested_stage_evidence(self):
        original = self.research()
        candidate = self.roles._read_ref(original['candidate_refs'][0])
        task_id = self.current['task_id']
        self.store.control(task_id, 'pause')
        self.store.control(task_id, 'resume')
        self.assertEqual(self.roles._candidates(self.context('maa', {'research': original})), [])
        dispatch = self.store.adopt_research(task_id, candidate['research_dispatch_id'], 'adopt-real-download',
                                            input_task='自主导入')
        adopted = self.store.dispatch_result(dispatch['dispatch_id'])
        self.assertEqual(self.roles._candidates(self.context('maa', {'research': adopted}))[0]['sha256'], candidate['sha256'])
        Path(candidate['goal_stage']['evidence_ref']['path']).write_text('{}', encoding='utf-8')
        self.assertEqual(self.roles._candidates(self.context('maa', {'research': adopted})), [])

    def test_real_code_cannot_be_typed_into_unrelated_resource_field(self):
        result = self.research()
        self.bind([snapshot(1, element('quantity', '理智药数量', ['set_value']))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='quantity', action='set_value', value='123'),
            call('finish_role', summary='字段不支持', status='unknown')]
        self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])

    def test_terminal_tool_in_batch_prevents_all_gui_effects(self):
        self.bind([snapshot(1, element('import', '导入', ['invoke']))])
        terminal = call('finish_role', status='unknown', summary='停止本角色')
        terminal['tool_calls'].extend(call('maa_act', element_id='import', action='invoke', value=None)['tool_calls'])
        self.roles.clients['maa'].responses = [call('inspect_maa'), terminal]
        self.roles.handlers['maa'](self.context('maa'), self.cancel)
        self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])

    def test_foreign_snapshot_cannot_authorize_input(self):
        wrong = snapshot(1, element('import', '导入', ['invoke']))
        wrong['window']['pid'] = 999
        self.bind([wrong])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='import', action='invoke', value=None),
            call('finish_role', summary='身份不符', status='unknown')]
        self.roles.handlers['maa'](self.context('maa'), self.cancel)
        self.assertFalse([c for c in self.roles.supervisor.commands if c['op'] == 'act'])

    def test_stale_loaded_snapshot_is_not_fresh_import_evidence(self):
        result = self.research()
        stale = snapshot(2, element('loaded', '已加载 真实关卡作业'))
        stale['timestamp'] = time.time() - 60
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])), stale])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='导入成功', status='import_verified')]
        result = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(result['status'], 'import_verified')

    def test_readonly_editable_echo_is_not_independent_loaded_text(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])),
                   snapshot(2, element('box', '真实关卡作业', ['set_value'], read_only=True))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'), call('finish_role', summary='已加载', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(actual['status'], 'import_verified')

    def test_edit_after_verification_invalidates_previous_import_proof(self):
        result = self.research()
        self.bind([snapshot(1, element('box', '作业码', ['set_value'])),
                   snapshot(2, element('loaded', '已加载 真实关卡作业'),
                            element('box', '作业码', ['set_value']))])
        self.roles.clients['maa'].responses = [call('inspect_maa'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('verify_import'),
            call('maa_act', element_id='box', action='set_value', value='123'),
            call('finish_role', summary='复用旧证据', status='import_verified')]
        actual = self.roles.handlers['maa'](self.context('maa', {'research': result}), self.cancel)
        self.assertNotEqual(actual['status'], 'import_verified')

    def idle_snapshot(self, number):
        return snapshot(number, element('start', '开始', ['invoke']),
                        element('task-status', '已停止', automation_id='TaskStatus', control_type='50017'))

    def test_stop_confirms_only_independent_idle_readback_and_releases_stop_barrier(self):
        self.bind([self.idle_snapshot(1)])
        current = self.store.control(self.current['task_id'], 'pause')
        result = self.roles.stop(current['task_id'], control_seq=current['control_seq'])
        self.assertTrue(result['external_stop_confirmed'])
        self.assertEqual(self.store.pending_operations(), [])
        self.assertEqual(self.store.slot_state()['status'], 'idle')
        self.assertTrue(self.roles.supervisor.revoked, 'only trusted Continue may reopen ordinary writes')

    def test_start_alone_and_unrelated_idle_title_cannot_confirm_stop(self):
        self.bind([snapshot(1, element('start', '开始', ['invoke']), element('copilot-title', '已停止'))])
        current = self.store.control(self.current['task_id'], 'pause')
        result = self.roles.stop(current['task_id'], control_seq=current['control_seq'])
        self.assertFalse(result['external_stop_confirmed'])
        self.assertEqual(self.store.slot_state()['status'], 'unknown')

    def test_trusted_resume_clears_revocation_after_idle_evidence_and_no_pending_effects(self):
        self.bind([self.idle_snapshot(1), self.idle_snapshot(2)])
        paused = self.store.control(self.current['task_id'], 'pause')
        self.roles.stop(paused['task_id'], control_seq=paused['control_seq'])
        self.store.control(paused['task_id'], 'resume')
        context = self.context('maa')
        self.roles._observe(context, {'evidence_refs': []}, self.cancel)
        self.assertFalse(self.roles.supervisor.revoked)
        self.assertEqual(len(self.roles.supervisor.reconciliations), 1)
        evidence = self.roles.supervisor.reconciliations[0]
        self.assertEqual(evidence['epoch'], context['epoch'])
        self.assertTrue(Path(evidence['evidence_ref']['path']).is_file())

    def test_idle_readback_cannot_clear_supervisor_timeout_or_pending_unknown_operation(self):
        self.bind([self.idle_snapshot(1), self.idle_snapshot(2)])
        self.roles.supervisor.revoked = self.roles.supervisor.unknown = True
        context = self.context('maa')
        self.roles._observe(context, {'evidence_refs': []}, self.cancel)
        self.assertEqual(self.roles.supervisor.reconciliations, [])
        self.roles.supervisor.unknown = False
        operation = self.store.prepare_operation(context['task_id'], context['dispatch_id'],
                                                 'unresolved-effect', {'kind': 'maa_import'})
        self.store.issue_operation(operation['operation_id'])
        self.store.enter_submission(operation['operation_id'])
        self.store.finish_operation(operation['operation_id'], 'unknown', {})
        self.roles._observe(context, {'evidence_refs': []}, self.cancel)
        self.assertEqual(self.roles.supervisor.reconciliations, [])
        self.assertEqual(self.store.slot_state()['status'], 'unknown')

    def test_confirmed_stop_does_not_clear_unknown_start(self):
        current = self.store.submit('离线开始边界替身', {'allow_start': True, 'attempt_limit': 1})
        dispatch = self.store.ensure_dispatch(current['task_id'], 'maa', 'offline-start', {})
        operation = self.store.prepare_operation(current['task_id'], dispatch['dispatch_id'],
                                                 'unknown-start', {'kind': 'start'}, starts=True)
        self.store.issue_operation(operation['operation_id'])
        self.store.enter_submission(operation['operation_id'])
        self.store.finish_operation(operation['operation_id'], 'unknown', {})
        self.bind([self.idle_snapshot(1)])
        paused = self.store.control(current['task_id'], 'pause')
        result = self.roles.stop(current['task_id'], control_seq=paused['control_seq'])
        self.assertTrue(result['external_stop_confirmed'])
        self.assertEqual(self.store.slot_state()['barrier_operation_id'], operation['operation_id'])
        self.assertEqual(self.store.slot_state()['attempts_reserved'], 1)


if __name__ == '__main__':
    unittest.main()


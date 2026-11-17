"""Real graph/checkpoint and application-journal recovery; no model or GUI."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore
from shu.maa_graph import MaaWorkflow, JsonOnlySerializer


class SimulatedCrash(BaseException):
    pass


class GraphRecovery(unittest.TestCase):
    def test_compound_chain_advances_only_after_durable_import(self):
        from shu.battle_contract import authorize
        for import_verified in (True, False):
            with self.subTest(import_verified=import_verified):
                goal = '给我找TL-EX-3普通的作业，导入MAA后开始执行'
                state = self.store.submit(goal, authorize({'goal': goal, 'user_request': goal,
                    'current_deliverable': 'maa_import',
                    'task_spec': {'kind': 'repeat_stage', 'stage_code': 'TL-EX-3'}}))
                calls = []
                path = self.directory / 'candidate.json'
                path.write_text('{}', encoding='utf-8')
                reference = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

                def catalog(context, cancel):
                    calls.append('catalog')
                    return {'status': 'researched'}

                def research(context, cancel):
                    calls.append('research')
                    return {'status': 'researched', 'candidate_refs': [reference]}

                def maa(context, cancel):
                    step = context['execution_step']
                    calls.append(step)
                    if step == 'import':
                        return {'status': 'import_verified' if import_verified else 'unknown',
                                'code': 'preview_not_verified', 'evidence_refs': [{'path': 'preview'}],
                                'battle_start_submitted': False, 'transport': 'screenshot_keyboard'}
                    self.assertIsNotNone(self.store.dispatch_result(context['import_dispatch_id']))
                    return {'status': 'battle_finished', 'battle_start_submitted': True,
                            'external_idle_confirmed': True, 'start_operation_id': 'fixture',
                            'evidence_refs': [{'path': 'battle'}]}

                graph = MaaWorkflow(self.store, self.directory, {'catalog': catalog, 'research': research, 'maa': maa})
                self.addCleanup(graph.close)
                with patch('shu.planned_agents.register_import_plan'), patch(
                        'shu.battle_import_binding.load_current_import_binding', return_value=object(), create=True):
                    result = graph.run(state['task_id'])
                    self.assertEqual(result['status'], 'battle_finished' if import_verified else 'needs_input')
                    expected = ['catalog', 'research', 'import'] + (['battle'] if import_verified else [])
                    self.assertEqual(calls, expected)
                    graph.run(state['task_id'])
                    self.assertEqual(calls, expected)

    def test_multi_job_research_cannot_finish_from_single_import_receipt(self):
        goal = '把测试活动的两个作业导入 MAA'
        state = self.store.submit(goal, {'goal':goal,'user_request':goal,
            'current_deliverable':'maa_import','source_task_id':self.task['task_id'],
            'allow_start':False,'attempt_limit':0,'task_spec':{'kind':'import'}})
        path = self.directory/'batch-queue.json'
        path.write_text('{}',encoding='utf-8')
        ref = {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        calls = []

        def research(context,cancel):
            return {'status':'researched','queue_plan_ref':ref,'target_count':2,
                    'candidate_refs':[ref,ref]}

        def maa(context,cancel):
            calls.append(context['task'])
            return {'status':'import_verified','evidence_refs':[ref],'battle_start_submitted':False}

        graph = MaaWorkflow(self.store,self.directory,{'research':research,'maa':maa})
        self.addCleanup(graph.close)
        result = graph.run(state['task_id'])
        self.assertEqual(len(calls),1)
        self.assertIn('多作业',calls[0])
        self.assertFalse(result['goal_complete'])
        self.assertEqual(result['code'],'queue_receipt_unverified')

    def test_typed_import_failure_gets_one_readonly_diagnosis_without_replaying_maa(self):
        goal = '将已查询的 A-1 普通作业导入 MAA'
        state = self.store.submit(goal, {'goal': goal, 'user_request': goal,
            'current_deliverable': 'maa_import', 'source_task_id': self.task['task_id'],
            'allow_start': False, 'attempt_limit': 0,
            'task_spec': {'kind': 'import', 'stage_code': 'A-1'}})
        path = self.directory / 'candidate.json'
        path.write_text('{}', encoding='utf-8')
        reference = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        calls = []
        failure = {'status': 'unknown', 'code': 'maa_import_readback_unverified',
                   'summary': '作业已提交，加载结果尚未核实。',
                   'action_outcome': 'unknown', 'evidence_refs': [reference],
                   'battle_start_submitted': False}

        def research(context, cancel):
            calls.append('research')
            return {'status': 'researched', 'candidate_refs': [reference]}

        def maa(context, cancel):
            calls.append('maa')
            return failure

        def diagnosis(context, cancel):
            calls.append('diagnosis')
            self.assertEqual(context['role_results']['maa'], failure)
            self.assertFalse(context['contract']['allow_start'])
            self.assertIn('只读', context['task'])
            return {'status': 'diagnosed', 'summary': '控件路径已更新，预览仍未完成加载。',
                    'evidence_refs': [reference]}

        graph = MaaWorkflow(self.store, self.directory,
                            {'research': research, 'maa': maa, 'diagnosis': diagnosis})
        self.addCleanup(graph.close)
        result = graph.run(state['task_id'])
        self.assertEqual(calls, ['research', 'maa', 'diagnosis'])
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['code'], failure['code'])
        self.assertFalse(result['goal_complete'])
        self.assertEqual(result['role_results']['maa'], failure)
        self.assertIn(failure['summary'], result['summary'])
        self.assertIn('预览仍未完成加载', result['summary'])
        self.assertEqual(graph.run(state['task_id']), result)
        self.assertEqual(calls, ['research', 'maa', 'diagnosis'])

    def test_typed_import_uses_catalog_and_single_step_plan_before_maa(self):
        from shu.leader_plan import PlanStore

        goal = '查找 A-1 的普通作业并导入 MAA'
        contract = {'goal': goal, 'user_request': goal, 'current_deliverable': 'maa_import',
                    'allow_start': False, 'attempt_limit': 0,
                    'task_spec': {'kind': 'import', 'stage_code': 'A-1'}}
        state = self.store.submit(goal, contract)
        calls = []

        def reference(name, value):
            raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
            path = self.directory / name
            path.write_bytes(raw)
            return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

        def catalog(context, cancel):
            calls.append('catalog')
            raw = reference('catalog.json', {'catalog_complete': True, 'event_name': '测试活动',
                            'source': 'fixture', 'raw_sha256': 'a' * 64})
            observation = reference('catalog-observation.json', {'task_id': context['task_id'],
                'epoch': context['epoch'], 'tool': 'research_activity_stages',
                'result': {'ok': True, 'stage_catalog_ref': raw}})
            stages = [{'stage_key': key, 'stage_id': key, 'display_code': 'A-1', 'difficulty': difficulty,
                       'group_id': 'g',
                       'query_key_verified': True, 'difficulty_verified': True}
                      for key, difficulty in [('ordinary', 1), ('challenge', 2)]]
            structure = reference('structure.json', {'ok': True, 'catalog_complete': True,
                'activity_name': '测试活动', 'catalog_ref': raw, 'stages': stages,
                'groups': [{'group_id': 'g', 'label': '区域', 'stage_keys': ['ordinary', 'challenge']}]})
            return {'status': 'completed', 'structure_ref': structure, 'evidence_refs': [observation]}

        def research(context, cancel):
            calls.append('research')
            plan = PlanStore(self.store).current(context['task_id'])
            self.assertEqual([step['stage_key'] for step in plan['steps']], ['ordinary'])
            return {'status': 'researched', 'candidate_refs': [reference('candidate.json', {})]}

        def maa(context, cancel):
            calls.append('maa')
            self.assertFalse(context['contract']['allow_start'])
            return {'status': 'import_verified', 'evidence_refs': ['ui-readback'],
                    'battle_start_submitted': False}

        graph = MaaWorkflow(self.store, self.directory,
                            {'catalog': catalog, 'research': research, 'maa': maa})
        self.addCleanup(graph.close)
        result = graph.run(state['task_id'])
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertEqual(calls, ['catalog', 'research', 'maa'])

    def test_pending_daily_does_not_route_to_copilot_roles(self):
        state = self.store.submit('清日常', {'goal': '清日常', 'current_deliverable': 'gameplay',
                                  'task_spec': {'kind': 'unknown'}, 'allow_start': False})
        graph = MaaWorkflow(self.store, self.directory, {})
        self.addCleanup(graph.close)
        result = graph.run(state['task_id'])
        self.assertEqual(result['code'], 'execution_not_available')
        self.assertEqual(result['completed_dispatch_ids'], [])

    def test_import_slot_is_bound_to_import_clause(self):
        from shu.planned_agents import _import_slot

        self.assertEqual(_import_slot('查主选和备选，只导入主选'), 'primary')
        self.assertEqual(_import_slot('先查主选，把备选作业导入 MAA'), 'backup')
        self.assertEqual(_import_slot('导入 A-1 的备选作业'), 'backup')
        with self.assertRaises(ValueError):
            _import_slot('导入主选和备选')

    def test_leader_service_error_survives_graph_end_without_becoming_missing_account_data(self):
        graph=MaaWorkflow(self.store,self.directory,{'leader':lambda ctx,cancel:
            {'next_role':'ask','code':'provider_http_503','summary':'模型服务暂不可用（HTTP 503）；未执行操作。'}})
        self.addCleanup(graph.close)
        result=graph.run(self.task['task_id'])
        self.assertEqual(result.get('code'),'provider_http_503')
        self.assertIn('HTTP 503',result['summary'])
        self.assertEqual(result['status'],'needs_input')

    def test_pure_lookup_goal_can_end_at_real_research_without_maa_import(self):
        state=self.store.submit('仅查询公开作业资料',{'goal':'仅查询公开作业资料','task_spec':{'kind':'lookup'}})
        def leader(ctx,cancel):
            return {'next_role':'complete'} if ctx.get('role_results') else {'next_role':'research','task':'只读查询资料'}
        graph=MaaWorkflow(self.store,self.directory,{'leader':leader,'research':lambda ctx,cancel:
            {'status':'researched','summary':'目录已返回','evidence_refs':[{'path':'fixture-readonly','sha256':'f'*64}]}})
        self.addCleanup(graph.close)
        result=graph.run(state['task_id'])
        self.assertEqual(result['status'],'research_verified')
        self.assertTrue(result['goal_complete'])
        self.assertNotIn('maa',result['role_results'])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.engine = TaskEngine(self.directory)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        self.task = self.store.submit('导入 TL-2，不开始', {'goal': 'TL-2', 'allow_start': False})
        self.calls = {'leader': 0, 'research': 0, 'maa': 0, 'diagnosis': 0}

    def handlers(self):
        def leader(context, cancel):
            self.calls['leader'] += 1
            results = context.get('role_results', {})
            if 'research' not in results:
                return {'next_role': 'research', 'task': 'Find a real applicable job'}
            if 'maa' not in results:
                return {'next_role': 'maa', 'task': 'Import the discovered job'}
            return {'next_role': 'complete', 'summary': 'Finished'}
        def research(context, cancel):
            self.calls['research'] += 1
            return {'status': 'completed', 'artifact_ref': 'synthetic-public-job'}
        def maa(context, cancel):
            self.calls['maa'] += 1
            return {'status': 'import_verified', 'evidence_refs': ['new-ui-readback'],
                    'battle_start_submitted': False, 'summary': 'Loaded and read back'}
        return {'leader': leader, 'research': research, 'maa': maa}

    def test_committed_research_survives_checkpoint_loss_without_second_call(self):
        def crash(kind, value):
            if kind == 'dispatch_completed' and value['role'] == 'research':
                raise SimulatedCrash()
        first = MaaWorkflow(self.store, self.directory, self.handlers(), emit=crash)
        with self.assertRaises(SimulatedCrash):
            first.run(self.task['task_id'])
        first.close()
        second = MaaWorkflow(self.store, self.directory, self.handlers())
        self.addCleanup(second.close)
        result = second.run(self.task['task_id'])
        self.assertEqual(self.calls['research'], 1)
        self.assertEqual(self.calls['maa'], 1)
        self.assertEqual(result['status'], 'import_verified')
        self.assertTrue((self.directory / 'maa-graph.sqlite3').is_file())

    def test_activity_shows_real_dispatch_boundaries_with_current_identity(self):
        events = []
        graph = MaaWorkflow(self.store, self.directory, self.handlers(),
                            emit=lambda kind, details: events.append((kind, details)))
        self.addCleanup(graph.close)
        graph.run(self.task['task_id'])
        research = [(kind, data) for kind, data in events if data['role'] == 'research']
        self.assertEqual([kind for kind, data in research], ['dispatch_started', 'dispatch_completed'])
        self.assertEqual(research[0][1]['dispatch_id'], research[1][1]['dispatch_id'])
        for kind, data in research:
            self.assertEqual(data['task_id'], self.task['task_id'])
            self.assertEqual(data['epoch'], self.store.current(self.task['task_id'])['epoch'])
            self.assertNotIn('role_results', data)
            self.assertNotIn('contract', data)
        self.assertEqual(research[1][1]['status'], 'completed')

    def test_pause_restart_explicit_leader_adoption_avoids_second_research(self):
        path = self.directory / 'published-copilot.json'
        path.write_text('{"stage_name":"TL-2","actions":[{}]}', encoding='utf-8')
        ref = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        handlers = self.handlers()
        original_leader = handlers['leader']
        def leader(context, cancel):
            history = context.get('historical_research', [])
            if history and 'research' not in context.get('role_results', {}):
                self.calls['leader'] += 1
                return {'next_role': 'adopt_research', 'source_dispatch_id': history[0]['dispatch_id'],
                        'task': history[0]['input_task']}
            return original_leader(context, cancel)
        def research(context, cancel):
            self.calls['research'] += 1
            return {'status': 'completed', 'candidate_refs': [ref]}
        handlers.update(leader=leader, research=research)
        def crash(kind, value):
            if kind == 'dispatch_completed' and value['role'] == 'research':
                raise SimulatedCrash()
        graph = MaaWorkflow(self.store, self.directory, handlers, emit=crash)
        with self.assertRaises(SimulatedCrash):
            graph.run(self.task['task_id'])
        graph.close()
        # Actual product startup revokes old authority; trusted continue advances epoch again.
        self.store.control(self.task['task_id'], 'pause')
        self.store.control(self.task['task_id'], 'resume')
        recovered = MaaWorkflow(self.store, self.directory, handlers)
        self.addCleanup(recovered.close)
        result = recovered.run(self.task['task_id'])
        self.assertEqual(result['status'], 'import_verified')
        self.assertEqual(self.calls['research'], 1)
        self.assertEqual(self.calls['maa'], 1)
        self.assertIn('adoption', result['role_results']['research'])

    def test_abandoned_running_research_routes_failure_to_leader_diagnosis(self):
        handlers = self.handlers()
        def interrupted(context, cancel):
            self.calls['research'] += 1
            raise SimulatedCrash()
        handlers['research'] = interrupted
        graph = MaaWorkflow(self.store, self.directory, handlers)
        with self.assertRaises(SimulatedCrash):
            graph.run(self.task['task_id'])
        graph.close()
        route = []
        def leader(context, cancel):
            route.append('leader')
            result = context.get('role_results', {}).get('research', {})
            self.assertEqual(result.get('code'), 'owner_exited')
            if 'diagnosis' not in context.get('role_results', {}):
                return {'next_role': 'diagnosis', 'task': 'Examine interrupted readonly request'}
            return {'next_role': 'ask', 'summary': 'Recovery diagnosed'}
        def diagnosis(context, cancel):
            route.append('diagnosis')
            return {'status': 'completed', 'summary': 'Original owner is no longer running'}
        recovered = MaaWorkflow(self.store, self.directory, {'leader': leader, 'diagnosis': diagnosis})
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.run(self.task['task_id'])['status'], 'needs_input')
        self.assertEqual(route, ['leader', 'diagnosis', 'leader'])
        self.assertEqual(self.calls['research'], 1)

    def test_abandoned_leader_gets_new_bounded_review_dispatch(self):
        def interrupted(context, cancel):
            raise SimulatedCrash()
        graph = MaaWorkflow(self.store, self.directory, {'leader': interrupted})
        with self.assertRaises(SimulatedCrash):
            graph.run(self.task['task_id'])
        graph.close()
        seen = []
        def leader(context, cancel):
            seen.append(context['interrupted_dispatches'])
            return {'next_role': 'ask', 'summary': 'Interrupted Leader reviewed'}
        recovered = MaaWorkflow(self.store, self.directory, {'leader': leader})
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.run(self.task['task_id'])['status'], 'needs_input')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0]['role'], 'leader')

    def test_committed_result_uses_frozen_input_after_registered_box_changes(self):
        from shu.maa_facts import RosterStore
        roster = RosterStore(self.engine)
        roster.publish('profile', [], True, {'kind': 'fixture', 'observed_at': 100, 'evidence_ref': 'fixture:1'})
        self.store.submit('TL-2', {'goal': 'TL-2', 'allow_start': False, 'profile_id': 'profile'}, self.task['task_id'])
        def crash(kind, value):
            if kind == 'dispatch_completed' and value['role'] == 'research':
                raise SimulatedCrash()
        graph = MaaWorkflow(self.store, self.directory, self.handlers(), emit=crash)
        with self.assertRaises(SimulatedCrash):
            graph.run(self.task['task_id'])
        graph.close()
        roster.publish('profile', [], True, {'kind': 'fixture', 'observed_at': 101, 'evidence_ref': 'fixture:2'})
        recovered = MaaWorkflow(self.store, self.directory, self.handlers())
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.run(self.task['task_id'])['status'], 'import_verified')
        self.assertEqual(self.calls['research'], 1)

    def test_leader_cannot_finish_without_actual_maa_receipt(self):
        graph = MaaWorkflow(self.store, self.directory,
                            {'leader': lambda ctx, cancel: {'next_role': 'complete', 'summary': 'trust me'}})
        self.addCleanup(graph.close)
        self.assertEqual(graph.run(self.task['task_id'])['status'], 'needs_input')

    def test_import_receipt_does_not_complete_requested_battle_repetitions(self):
        contract = {'goal':'导入 TL-10 后开始作战六次，不使用理智药', 'allow_start':False,
                    'task_spec':{'kind':'repeat_stage','stage_code':'TL-10','repeat_count':6,
                                 'medicine_limit':0,'premium_limit':0,'stop_after_current':False,'unresolved':[]}}
        self.task = self.store.submit(contract['goal'], contract)
        graph = MaaWorkflow(self.store, self.directory, self.handlers())
        self.addCleanup(graph.close)
        result = graph.run(self.task['task_id'])
        self.assertEqual(result['status'], 'needs_input')
        self.assertFalse(result.get('goal_complete', True))
        self.assertTrue(result.get('pending_objectives'))
        self.assertEqual(self.calls['maa'], 1)

    def test_worker_handoffs_are_visible_to_leader_with_role_task_and_evidence(self):
        seen = []
        def leader(ctx, cancel):
            seen.append(ctx)
            if not ctx.get('role_results'):
                return {'next_role':'maa','task':'只读检查 MAA'}
            return {'next_role':'ask','summary':'只读事实已核对，等待后续目标'}
        def maa(ctx, cancel):
            return {'status':'observed','summary':'MAA 当前空闲','evidence_refs':['fixture-readback']}
        graph = MaaWorkflow(self.store, self.directory, {'leader':leader,'maa':maa})
        self.addCleanup(graph.close)
        graph.run(self.task['task_id'])
        history = seen[-1]['handoff_history']
        self.assertEqual(history[-1]['role'], 'maa')
        self.assertEqual(history[-1]['task'], '只读检查 MAA')
        self.assertEqual(history[-1]['result']['evidence_refs'], ['fixture-readback'])
        self.assertEqual(seen[-1]['last_role'], 'maa')

    def test_repeated_identical_unproductive_handoffs_end_with_specific_blocker(self):
        counts = {'maa':0}
        def leader(ctx, cancel):
            return {'next_role':'maa','task':'重复同一个无法执行的步骤'}
        def maa(ctx, cancel):
            counts['maa'] += 1
            return {'status':'unknown','code':'no_new_observation','summary':'没有新的控件证据'}
        graph = MaaWorkflow(self.store,self.directory,{'leader':leader,'maa':maa})
        self.addCleanup(graph.close)
        result = graph.run(self.task['task_id'])
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(result.get('code'),'no_progress')
        self.assertLessEqual(counts['maa'],3)

    def test_terminal_checkpoint_retries_outbox_ack_without_any_role_call(self):
        graph = MaaWorkflow(self.store, self.directory, self.handlers())
        self.addCleanup(graph.close)
        real_ack = self.store.ack_event
        def lost_ack(event_id):
            raise SimulatedCrash()
        self.store.ack_event = lost_ack
        with self.assertRaises(SimulatedCrash):
            graph.run(self.task['task_id'])
        calls_before = dict(self.calls)
        self.store.ack_event = real_ack
        self.assertEqual(graph.run(self.task['task_id'])['status'], 'import_verified')
        self.assertEqual(self.calls, calls_before)
        self.assertEqual([e for e in self.store.pending_events(self.task['task_id'])
                          if e['type'].startswith('dispatch.')], [])

    def test_result_committed_after_epoch_change_is_historical_not_applied(self):
        handlers = self.handlers()
        def stale_research(ctx, cancel):
            self.store.control(self.task['task_id'], 'pause')
            return {'status': 'completed', 'artifact_ref': 'historical-only'}
        handlers['research'] = stale_research
        graph = MaaWorkflow(self.store, self.directory, handlers)
        self.addCleanup(graph.close)
        from shu.core import ActionRejected
        with self.assertRaises(ActionRejected):
            graph.run(self.task['task_id'])
        with self.engine._lock:
            row = self.engine._db.execute("SELECT dispatch_id FROM workflow_dispatches WHERE role='research'").fetchone()
        self.assertEqual(self.store.dispatch_result(row[0], current_only=False)['artifact_ref'], 'historical-only')
        self.assertIsNone(self.store.dispatch_result(row[0]))
        self.assertEqual(self.calls['maa'], 0)

    def test_worker_failure_goes_to_diagnosis_then_leader_not_direct_write(self):
        route = []
        def leader(ctx, cancel):
            route.append('leader')
            if 'maa' not in ctx.get('role_results', {}):
                return {'next_role': 'maa', 'task': 'inspect'}
            if 'diagnosis' not in ctx.get('role_results', {}):
                return {'next_role': 'diagnosis', 'task': 'explain real failure'}
            return {'next_role': 'ask', 'summary': 'MAA needs a verified permission step'}
        def failed(ctx, cancel):
            route.append('maa')
            return {'status': 'failed', 'code': 'permission_mismatch'}
        def diagnosis(ctx, cancel):
            route.append('diagnosis')
            return {'status': 'completed', 'summary': 'permission mismatch', 'proposed_repair': 'rebind'}
        graph = MaaWorkflow(self.store, self.directory,
                            {'leader': leader, 'maa': failed, 'diagnosis': diagnosis})
        self.addCleanup(graph.close)
        self.assertEqual(graph.run(self.task['task_id'])['status'], 'needs_input')
        self.assertEqual(route, ['leader', 'maa', 'leader', 'diagnosis', 'leader'])

    def test_paused_epoch_does_not_call_worker_on_resume_of_old_checkpoint(self):
        graph = MaaWorkflow(self.store, self.directory, self.handlers())
        self.addCleanup(graph.close)
        self.store.control(self.task['task_id'], 'pause')
        self.assertEqual(graph.run(self.task['task_id'])['status'], 'paused')
        self.assertFalse(any(self.calls.values()))

    def test_serializer_refuses_reasoning_or_arbitrary_objects(self):
        serializer = JsonOnlySerializer()
        for value in ({'reasoning_content': 'hidden'}, {'api_key': 'secret'}, {'object': object()}):
            with self.assertRaises((ValueError, TypeError)):
                serializer.dumps_typed(value)
        self.assertEqual(serializer.loads_typed(serializer.dumps_typed({'phase': 'research'})),
                         {'phase': 'research'})


if __name__ == '__main__':
    unittest.main()

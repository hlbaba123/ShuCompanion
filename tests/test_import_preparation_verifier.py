"""B0 completion is proved by real local receipts, never role prose or GUI."""
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from shu.import_preparation import verify_import_preparation_result
from shu.research_tools import ResearchTools


class PreparationVerifierTests(unittest.TestCase):
    def setUp(self):
        from tests.test_import_preparation import ImportPreparationTests
        self.fixture=ImportPreparationTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.workflow=self.fixture.workflow
        self.context=self.fixture.new_context(task_spec={'kind':'import'},allow_start=False,attempt_limit=0)
        self.fixture.context=self.context
        self.prepared=self.fixture.prepare()
        self.receipt={'status':'researched','candidate_refs':[self.prepared['candidate_ref']],
                      'evidence_refs':[self.prepared['candidate_ref']]}
        self.workflow.finish_dispatch(self.context['dispatch_id'],self.receipt)

    def check(self):
        return verify_import_preparation_result(self.workflow,self.context['task_id'])

    def suspend_with_proof(self):
        state=self.workflow.suspend(self.context['task_id'],expected_epoch=self.context['epoch'],
                                    message='已核验准备，等待用户验收。')
        with self.fixture.engine._transaction():
            self.workflow._publish_event(self.context['task_id'],'import.preparation_verified',{
                'research_dispatch_id':self.context['dispatch_id'],'research_epoch':self.context['epoch'],
                'candidate_refs':self.receipt['candidate_refs']},
                event_key='fixture:import-preparation:'+str(state['epoch']))
        return state

    def test_same_epoch_material_uses_no_constructor_writer_network_or_model(self):
        changes=self.fixture.engine._db.total_changes
        with patch('shu.import_preparation.ResearchTools') as research:
            result=self.check()
        research.assert_not_called()
        self.assertTrue(result['verified'],result)
        self.assertEqual(result['items'][0]['mystery_code'],'prts://42')
        self.assertFalse(result['goal_complete'])
        self.assertEqual(result['maa_dispatches'],0)
        self.assertEqual(result['submitted_operations'],0)
        self.assertEqual(self.fixture.engine._db.total_changes,changes)

    def test_revoked_next_epoch_requires_current_durable_b0_stop_event(self):
        state=self.workflow.suspend(self.context['task_id'],expected_epoch=self.context['epoch'],message='stopped')
        self.assertFalse(self.check()['verified'])
        with self.fixture.engine._transaction():
            self.workflow._publish_event(self.context['task_id'],'import.preparation_verified',{
                'research_dispatch_id':self.context['dispatch_id'],'research_epoch':self.context['epoch'],
                'candidate_refs':self.receipt['candidate_refs']},event_key='fixture-stop')
        before=self.workflow.current(self.context['task_id'])
        self.assertTrue(self.check()['verified'])
        self.assertEqual(self.workflow.current(self.context['task_id']),before)
        self.assertFalse(before['writes_enabled'])

    def test_prepared_source_box_and_document_are_rechecked_after_stop(self):
        self.suspend_with_proof()
        self.assertTrue(self.check()['verified'])
        self.fixture.publish_box(observed_at=1001)
        self.assertFalse(self.check()['verified'])

    def test_stop_and_preparation_event_roll_back_together(self):
        task_id = self.context['task_id']
        before = self.workflow.current(task_id)
        publish = self.workflow._publish_event

        def fail_preparation_event(owner, event_type, *args, **kwargs):
            if event_type == 'import.preparation_verified':
                raise OSError('simulated outbox write failure')
            return publish(owner, event_type, *args, **kwargs)

        with patch.object(self.workflow, '_publish_event', side_effect=fail_preparation_event):
            with self.assertRaises(OSError):
                self.workflow.suspend(task_id, expected_epoch=before['epoch'], message='准备已核实',
                                      preparation_dispatch_id=self.context['dispatch_id'])
        self.assertEqual(self.workflow.current(task_id), before)
        state = self.workflow.suspend(task_id, expected_epoch=before['epoch'], message='准备已核实',
                                      preparation_dispatch_id=self.context['dispatch_id'])
        self.assertFalse(state['writes_enabled'])
        self.assertTrue(self.check()['verified'])

    def test_tampered_document_is_not_rescued_by_role_success(self):
        path=Path(self.prepared['candidate_record']['path'])
        path.write_bytes(path.read_bytes()+b' ')
        self.assertFalse(self.check()['verified'])

    def test_maa_dispatch_or_paused_generation_cannot_pass_b0(self):
        self.workflow.ensure_dispatch(self.context['task_id'],'maa','not-allowed',{})
        result=self.check()
        self.assertFalse(result['verified'])
        self.assertEqual(result['maa_dispatches'],1)

    def test_pause_and_trash_never_become_verified_history(self):
        self.workflow.control(self.context['task_id'],'pause')
        self.assertFalse(self.check()['verified'])
        self.workflow.management.trash(self.context['task_id'])
        self.assertFalse(self.check()['verified'])

    def test_actual_two_mode_queue_plan_is_rebuilt_not_merely_hash_read(self):
        fixture=self.fixture
        fixture.stage.update(opening_status='permanent',navigation_region_id='public-zone:fixture')
        fixture.body['doc']['details']='测试说明'
        fixture.make_two_mode_source()
        context=fixture.new_context(user_request='把 A-1 普通和突袭的主选导入 MAA',
            task_spec={'kind':'import'},allow_start=False,attempt_limit=0)
        def transport(req,**kwargs):
            number=int(req.full_url.rsplit('/',1)[-1])
            return json.dumps({'status_code':200,'data':{'id':number,
                'content':{**fixture.body,'difficulty':1 if number==42 else 2}}}).encode()
        agents=SimpleNamespace(store=self.workflow,directory=fixture.root,
            _guard=lambda current,cancel:fixture.bridge._guard(current,cancel))
        from shu.planned_agents import run_import_research
        with patch('shu.import_preparation.ResearchTools',side_effect=
                   lambda config,cache,**kwargs:ResearchTools(config,cache,transport=transport,**kwargs)):
            receipt=run_import_research(agents,context,threading.Event())
        self.workflow.finish_dispatch(context['dispatch_id'],receipt)
        changes=fixture.engine._db.total_changes
        result=verify_import_preparation_result(self.workflow,context['task_id'])
        self.assertTrue(result['verified'],result)
        self.assertEqual([(item['difficulty'],item['mystery_code']) for item in result['items']],
                         [(1,'prts://42'),(2,'prts://43')])
        self.assertEqual(fixture.engine._db.total_changes,changes)

    def test_real_coordinator_stop_event_remains_independently_inspectable(self):
        from shu.maa_coordinator import MaaCoordinator,_Run
        from shu.runtime import RuntimeStore
        fixture=self.fixture
        contract=dict(self.context['contract'])
        state=self.workflow.submit(contract['goal'],contract)
        def research_handler(context,cancel):
            def transport(req,**kwargs):
                return json.dumps({'status_code':200,'data':{'id':42,'content':fixture.body}}).encode()
            research=ResearchTools(fixture.research().config,
                fixture.root/'research'/context['task_id']/context['dispatch_id'],transport=transport)
            prepared=fixture.bridge.prepare(context,source_task_id=fixture.source,
                step_id=fixture.step_id,research=research,cancel_event=cancel)
            return {'status':'researched','candidate_refs':[prepared['candidate_ref']],
                    'evidence_refs':[prepared['candidate_ref']]}
        runtime=RuntimeStore(fixture.root)
        self.addCleanup(runtime.close)
        coordinator=MaaCoordinator(fixture.engine,Mock(),runtime,role_config=None,task_config=None,
            permissions=lambda:(False,False),directory=fixture.root,store=self.workflow,recover=False)
        self.addCleanup(coordinator.close)
        maa=Mock(side_effect=AssertionError('B0 must not dispatch MAA'))
        job=_Run(state['task_id'],state['epoch'])
        job.roles=SimpleNamespace(handlers={'research':research_handler,'maa':maa},close=lambda:None)
        coordinator._roles_for=Mock(return_value=job.roles)
        coordinator._run(job)
        current=self.workflow.current(state['task_id'])
        self.assertEqual(current['status'],'needs_input')
        self.assertFalse(current['writes_enabled'])
        self.assertEqual(current['epoch'],state['epoch']+1)
        changes=fixture.engine._db.total_changes
        actual=verify_import_preparation_result(self.workflow,state['task_id'])
        self.assertTrue(actual['verified'],actual)
        self.assertEqual(fixture.engine._db.total_changes,changes)
        self.assertEqual(actual['items'][0]['mystery_code'],'prts://42')
        maa.assert_not_called()


if __name__=='__main__':
    unittest.main()

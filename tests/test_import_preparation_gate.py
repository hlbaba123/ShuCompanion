"""B0 product capability stops at verified preparations, never MAA dispatch."""
import hashlib
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore
from shu.maa_graph import MaaWorkflow


class ImportPreparationGateTests(unittest.TestCase):
    def test_single_stage_rejects_batch_before_any_dispatch(self):
        graph = MaaWorkflow(self.store,self.root,{'maa':self.maa},
                            allow_import_gui=True,single_import_only=True)
        self.addCleanup(graph.close)
        state={**self.state,'role_results':{'research':{
            'queue_plan_ref':{'path':'batch','sha256':'x'},'candidate_refs':[{},{}]}}}
        with self.assertRaises(ActionRejected):
            graph._call('maa',state)
        self.maa.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        source = self.store.submit('只读来源查询',{'current_deliverable':'copilot_codes'})
        goal = '将已查询的 TL-2 和 TL-9 普通作业导入 MAA'
        self.state = self.store.submit(goal,{'goal':goal,'user_request':goal,
            'current_deliverable':'maa_import','source_task_id':source['task_id'],
            'allow_start':False,'attempt_limit':0,'task_spec':{'kind':'import'}})
        proof_path = self.root/'candidate.json'
        proof_path.write_bytes(b'{}')
        self.research = {'status':'researched','candidate_refs':[
            {'path':str(proof_path),'sha256':hashlib.sha256(proof_path.read_bytes()).hexdigest()}]}
        self.maa = Mock(side_effect=AssertionError('MAA must not be dispatched in B0'))
        self.proof = {'verified':True,'goal_complete':False,'maa_dispatches':0,'submitted_operations':0,
            'items':[{'stage_code':'TL-2','difficulty':1,'mystery_code':'prts://123'},
                     {'stage_code':'TL-9','difficulty':1,'mystery_code':'prts://456'}]}

    def graph(self, *, allow_import_gui=False):
        graph = MaaWorkflow(self.store,self.root,
            {'research':lambda ctx,cancel:self.research,'maa':self.maa},
            allow_import_gui=allow_import_gui)
        self.addCleanup(graph.close)
        return graph

    def test_verified_preparations_are_visible_but_do_not_complete_import(self):
        def verify(workflow,task_id):
            self.assertIs(workflow,self.store)
            self.assertEqual(task_id,self.state['task_id'])
            rows = self.engine._db.execute("SELECT * FROM workflow_dispatches WHERE task_id=? AND role='research'",
                                           (task_id,)).fetchall()
            self.assertEqual(len(rows),1)
            dispatch = self.store._dispatch(rows[0])
            self.assertEqual(dispatch['status'],'completed')
            self.assertEqual(self.store.dispatch_result(dispatch['dispatch_id']),self.research)
            return {**self.proof,'research_dispatch_id':dispatch['dispatch_id']}
        with patch('shu.import_preparation.verify_import_preparation_result',side_effect=verify,create=True):
            graph = self.graph()
            result = graph.run(self.state['task_id'])
            self.assertEqual(graph.run(self.state['task_id']),result)
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(result['code'],'import_preparation_verified')
        self.assertFalse(result['goal_complete'])
        self.assertEqual(result['waiting_kind'],'blocked')
        self.assertIn('TL-2：prts://123',result['summary'])
        self.assertIn('TL-9：prts://456',result['summary'])
        self.assertIn('尚未导入',result['summary'])
        self.maa.assert_not_called()
        self.assertEqual(self.engine._db.execute("SELECT COUNT(*) FROM workflow_dispatches WHERE task_id=? AND role='maa'",
                                               (self.state['task_id'],)).fetchone()[0],0)
        self.assertEqual(self.store.pending_operations(self.state['task_id']),[])

    def test_product_boundary_persists_exact_source_event_after_revoking_writes(self):
        from shu.maa_coordinator import MaaCoordinator,_Run
        from shu.runtime import RuntimeStore
        from types import SimpleNamespace
        runtime = RuntimeStore(self.root)
        self.addCleanup(runtime.close)
        coordinator = MaaCoordinator(self.engine,Mock(),runtime,role_config=None,task_config=None,
            permissions=lambda:(False,False),directory=self.root,store=self.store,recover=False)
        self.addCleanup(coordinator.close)
        job = _Run(self.state['task_id'],self.state['epoch'])
        job.roles = SimpleNamespace(handlers={'research':lambda ctx,cancel:self.research,'maa':self.maa},close=lambda:None)
        coordinator._roles_for = Mock(return_value=job.roles)
        def verify(workflow,task_id):
            row = self.engine._db.execute("SELECT dispatch_id FROM workflow_dispatches WHERE task_id=? AND role='research'",
                                          (task_id,)).fetchone()
            return {**self.proof,'research_dispatch_id':row[0]}
        with patch('shu.import_preparation.verify_import_preparation_result',side_effect=verify):
            coordinator._run(job)
        current = self.store.current(self.state['task_id'])
        self.assertEqual(current['status'],'needs_input')
        self.assertFalse(current['writes_enabled'])
        self.assertEqual(current['epoch'],self.state['epoch']+1)
        events = [event for event in self.store.pending_events(self.state['task_id'])
                  if event['type']=='import.preparation_verified']
        self.assertEqual(len(events),1)
        event = events[0]
        self.assertEqual(event['epoch'],current['epoch'])
        self.assertEqual(event['payload']['candidate_refs'],self.research['candidate_refs'])
        self.assertEqual(event['payload']['research_epoch'],self.state['epoch'])
        self.maa.assert_not_called()

    def test_present_refs_without_current_verified_preparation_cannot_pass_gate(self):
        with patch('shu.import_preparation.verify_import_preparation_result',
                   return_value={'verified':False,'code':'prepared_source_changed','summary':'来源已变化'},create=True):
            result = self.graph().run(self.state['task_id'])
        self.assertFalse(result['goal_complete'])
        self.assertNotEqual(result['code'],'import_preparation_verified')
        self.assertIn('来源已变化',result['summary'])
        self.maa.assert_not_called()

    def test_gate_validation_exception_is_pending_and_never_routes_to_maa(self):
        with patch('shu.import_preparation.verify_import_preparation_result',
                   side_effect=ActionRejected('Box 已变化'),create=True):
            result = self.graph().run(self.state['task_id'])
        self.assertEqual(result['status'],'needs_input')
        self.assertEqual(result['code'],'import_preparation_unverified')
        self.assertFalse(result['goal_complete'])
        self.assertIn('Box',result['summary'])
        self.maa.assert_not_called()

    def test_guard_rejects_direct_or_replayed_maa_before_dispatch(self):
        graph = self.graph()
        with self.assertRaises(ActionRejected):
            graph._call('maa',{**self.state,'turn':0})
        self.assertEqual(self.engine._db.execute("SELECT COUNT(*) FROM workflow_dispatches WHERE task_id=?",
                                               (self.state['task_id'],)).fetchone()[0],0)
        self.maa.assert_not_called()

    def test_default_graph_capability_remains_available_to_explicit_legacy_tests(self):
        graph = MaaWorkflow(self.store,self.root,{'maa':lambda ctx,cancel:{'status':'probe'}})
        self.addCleanup(graph.close)
        self.assertIs(graph.allow_import_gui,True)

    def test_product_default_factory_closes_gui_but_custom_factories_keep_signature(self):
        from shu.maa_coordinator import MaaCoordinator,_Run
        from shu.runtime import RuntimeStore
        runtime = RuntimeStore(self.root)
        self.addCleanup(runtime.close)
        coordinator = MaaCoordinator(self.engine,Mock(),runtime,role_config=None,task_config=None,
            permissions=lambda:(False,False),directory=self.root,store=self.store,recover=False)
        self.addCleanup(coordinator.close)
        job = _Run(self.state['task_id'],self.state['epoch'])
        roles = Mock(handlers={})
        coordinator._roles_for = Mock(return_value=roles)
        workflow = Mock()
        workflow.run.return_value = {'status':'needs_input','summary':'test'}
        with patch('shu.maa_graph.MaaWorkflow',return_value=workflow) as factory:
            coordinator._run(job)
        self.assertIs(factory.call_args.kwargs['allow_import_gui'],False)
        custom = Mock(return_value=workflow)
        coordinator.workflow_factory = custom
        coordinator._run(_Run(self.state['task_id'],self.state['epoch']))
        self.assertNotIn('allow_import_gui',custom.call_args.kwargs)

    def test_real_preparation_product_event_can_be_verified_after_store_restart(self):
        from tests.test_import_preparation import ImportPreparationTests
        from shu.import_preparation import verify_import_preparation_result
        from shu.maa_coordinator import MaaCoordinator,_Run
        from shu.runtime import RuntimeStore
        from types import SimpleNamespace
        fixture = ImportPreparationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.context = fixture.new_context(allow_start=False,attempt_limit=0,task_spec={'kind':'import'})
        runtime = RuntimeStore(fixture.root)
        self.addCleanup(runtime.close)
        coordinator = MaaCoordinator(fixture.engine,Mock(),runtime,role_config=None,task_config=None,
            permissions=lambda:(False,False),directory=fixture.root,store=fixture.workflow,recover=False)
        self.addCleanup(coordinator.close)
        def research(context,cancel):
            fixture.context = context
            prepared = fixture.prepare()
            return {'status':'researched','candidate_refs':[prepared['candidate_ref']],
                    'evidence_refs':[prepared['candidate_ref']]}
        roles = SimpleNamespace(handlers={'research':research,'maa':self.maa},close=lambda:None)
        coordinator._roles_for = Mock(return_value=roles)
        initial = fixture.workflow.current(fixture.context['task_id'])
        coordinator._run(_Run(initial['task_id'],initial['epoch']))
        current = fixture.workflow.current(initial['task_id'])
        self.assertEqual(current['status'],'needs_input')
        proof = verify_import_preparation_result(fixture.workflow,initial['task_id'])
        self.assertTrue(proof['verified'],proof)
        displayed = coordinator.research_output(initial['task_id'])
        self.assertEqual(displayed['items'][0]['primary_code'],'prts://42')
        self.assertEqual(displayed['source_task_id'],initial['contract']['source_task_id'])
        self.assertTrue(displayed['query_complete'])
        self.assertFalse(displayed['complete'])
        self.assertFalse(displayed['goal_complete'])
        self.assertEqual([(row['stage_code'],row['mystery_code']) for row in proof['items']],
                         [('A-1','prts://42')])
        self.assertEqual(len(fixture.requests),1)
        coordinator.close()
        runtime.close()
        fixture.engine.close()
        reopened = TaskEngine(fixture.root)
        self.addCleanup(reopened.close)
        after_restart = verify_import_preparation_result(WorkflowStore(reopened),initial['task_id'])
        self.assertTrue(after_restart['verified'],after_restart)
        self.assertEqual(after_restart['candidate_refs'],proof['candidate_refs'])
        self.assertFalse(after_restart['goal_complete'])
        self.assertEqual(after_restart['maa_dispatches'],0)
        self.assertEqual(after_restart['submitted_operations'],0)
        self.maa.assert_not_called()


if __name__=='__main__':
    unittest.main()

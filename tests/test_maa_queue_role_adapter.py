"""Normal MAA role routing retains queue provenance before native work."""
import threading
from unittest.mock import Mock
import unittest

from shu.maa_agents import MaaRoleAgents
from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore


class QueueRoleAdapterTests(unittest.TestCase):
    def test_local_first_queue_adapter_retains_both_recognizers(self):
        from pathlib import Path
        from unittest.mock import patch

        from shu.maa_framework_observer import LocalFirstReadback
        from shu.maa_queue_readback import LocalFirstQueueReadback
        from shu.maa_visual_readback import MaaVisualReadback

        local_ocr, fallback_ocr = Mock(), Mock()
        reader = LocalFirstReadback(MaaVisualReadback(ocr=local_ocr),
                                   MaaVisualReadback(ocr=fallback_ocr))
        contract = {'current_deliverable':'maa_import', 'allow_start':False,
                    'attempt_limit':0, 'task_spec':{'kind':'import'}}
        candidate_refs = [{'path':'first'}, {'path':'second'}]
        plan = {'task_id':'task', 'revision':1, 'epoch':1,
                'targets':[{'candidate_ref':ref} for ref in candidate_refs]}
        plan_ref = {'path':'plan'}
        context = {'task_id':'task', 'revision':1, 'epoch':1, 'contract':contract,
                   'role_results':{'research':{'queue_plan_ref':plan_ref,
                                              'candidate_refs':candidate_refs}}}
        roles = MaaRoleAgents.__new__(MaaRoleAgents)
        roles.directory = Path('unused-test-directory')
        roles._supervisor_lock = threading.Lock()
        roles._guard = Mock()
        roles.screen_permission = lambda:True
        roles.store = Mock()
        roles.store.current.return_value = {'contract':contract}
        roles.store.pending_operations.return_value = []
        roles.store.get_dispatch.return_value = {'role':'research', 'task_id':'task'}
        roles.store.dispatch_result.return_value = {
            'queue_plan_ref':plan_ref, 'candidate_refs':candidate_refs}
        roles.supervisor = Mock(backend='visual')
        roles._import_vision_readback = Mock(return_value=reader)
        expected = {'status':'import_verified', 'code':'queue_verified',
                    'coverage':{'verified':2}, 'battle_start_submitted':False}
        records = [{'research_dispatch_id':'research'} for _ in candidate_refs]
        with patch('shu.leader_plan.read_reference', side_effect=[plan, *records]), \
                patch('shu.maa_visual_native.discover_maa_windows', return_value=[]), \
                patch('shu.maa_queue_store.QueueStore'), \
                patch('shu.import_preparation.ImportPreparation'), \
                patch('shu.maa_queue_import.QueueCodeImporter') as recipe:
            recipe.return_value.run.return_value = expected
            result = roles._run_queue_import(context, threading.Event())
        self.assertEqual(result['status'], 'import_verified')
        observer = recipe.call_args.kwargs['readback']
        self.assertIsInstance(observer, LocalFirstQueueReadback)
        self.assertIs(observer.local.ocr, local_ocr)
        self.assertIs(observer.fallback.ocr, fallback_ocr)
        self.assertFalse(result['battle_start_submitted'])

    def test_batch_handoff_uses_queue_role_entry(self):
        roles = MaaRoleAgents.__new__(MaaRoleAgents)
        roles._run_queue_import = Mock(return_value={'status':'needs_input','code':'probe'})
        roles._run_visual_import = Mock()
        context = {'contract':{'current_deliverable':'maa_import'},
                   'role_results':{'research':{'queue_plan_ref':{'path':'proof'}}}}
        cancel = threading.Event()
        self.assertEqual(roles._handler('maa')(context,cancel)['code'],'probe')
        roles._run_queue_import.assert_called_once_with(context,cancel)
        roles._run_visual_import.assert_not_called()

    def test_missing_current_research_receipt_blocks_before_window_discovery(self):
        import hashlib
        import json
        from pathlib import Path
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = TaskEngine(root)
            self.addCleanup(engine.close)
            store = WorkflowStore(engine)
            contract = {'current_deliverable':'maa_import','allow_start':False,'attempt_limit':0,
                        'task_spec':{'kind':'import'}}
            task = store.submit('导入两个关卡作业',contract)
            dispatch = store.ensure_dispatch(task['task_id'],'maa','current',{'contract':contract})
            store.claim_dispatch(dispatch['dispatch_id'],'test-owner')
            record = {'research_dispatch_id':'missing-research'}
            candidate = root/'candidate.json'
            candidate.write_text(json.dumps(record),encoding='utf-8')
            candidate_ref = {'path':str(candidate),'sha256':hashlib.sha256(candidate.read_bytes()).hexdigest()}
            plan = {**{key:task[key] for key in ('task_id','revision','epoch')},
                    'targets':[{'candidate_ref':candidate_ref},{'candidate_ref':candidate_ref}]}
            path = root/'plan.json'
            path.write_text(json.dumps(plan),encoding='utf-8')
            plan_ref = {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            context = {**task,'dispatch_id':dispatch['dispatch_id'],'role_results':{
                'research':{'queue_plan_ref':plan_ref,'candidate_refs':[candidate_ref,candidate_ref]}}}
            roles = MaaRoleAgents.__new__(MaaRoleAgents)
            roles.store, roles.directory, roles._closed = store,root,False
            roles.screen_permission = roles.input_permission = lambda:True
            with patch('shu.maa_visual_native.discover_maa_windows') as discover:
                result = roles._handler('maa')(context,threading.Event())
            self.assertEqual(result['status'],'needs_input')
            self.assertFalse(result['battle_start_submitted'])
            discover.assert_not_called()
            self.assertEqual(store.pending_operations(),[])
            engine.close()


if __name__=='__main__':
    unittest.main()

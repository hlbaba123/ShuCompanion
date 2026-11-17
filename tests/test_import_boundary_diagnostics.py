"""B0 diagnostic contracts, without a provider, network, MAA or game input."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu.core import ActionRejected, TaskEngine, TaskNotFound
from shu.import_preparation import import_boundary, ImportBoundaryTaskNotFound
from shu.workflow_store import WorkflowStore


class ImportBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory=Path(self.temporary.name)
        self.engine=TaskEngine(self.directory)
        self.addCleanup(self.engine.close)
        self.workflow=WorkflowStore(self.engine)
        self.state=self.workflow.submit('private user request',{
            'goal':'private user request','current_deliverable':'maa_import',
            'allow_start':False,'attempt_limit':0,'task_spec':{'kind':'import'}})
        self.context={**self.state,'dispatch_id':'actual-dispatch'}

    def test_task_identity_error_gets_safe_stack_and_current_database_facts(self):
        secret='sk-do-not-export-http-token'; raw='Bearer private-http-payload'
        def task_failure():
            raise TaskNotFound(secret+' '+raw)
        with self.assertRaises(ImportBoundaryTaskNotFound) as caught:
            with import_boundary(self.workflow,self.directory,self.context,'prepare_candidate'):
                task_failure()
        error=caught.exception
        self.assertEqual(error.code,'import_boundary_task_not_found')
        proof=json.loads(Path(error.diagnostic_ref['path']).read_text(encoding='utf-8'))
        self.assertTrue(proof['workflow_scope']['task']['task_present'])
        self.assertEqual(proof['task_id'],self.state['task_id'])
        self.assertEqual(proof['phase'],'prepare_candidate')
        self.assertTrue(any(frame['function']=='task_failure' for frame in proof['traceback']))
        self.assertTrue(all(set(frame)=={'file','function','line'} for frame in proof['traceback']))
        serialized=json.dumps(proof,ensure_ascii=False)+str(error)
        for private in (secret,raw,'private user request','raise TaskNotFound'):
            self.assertNotIn(private,serialized)

    def test_lifecycle_database_mismatch_is_visible_without_rebinding_or_retry(self):
        other=TaskEngine(self.directory/'other')
        self.addCleanup(other.close)
        self.workflow.management.engine=other
        with self.assertRaises(ImportBoundaryTaskNotFound) as caught:
            with import_boundary(self.workflow,self.directory,self.context,'source_plan'):
                self.workflow.management.require_active(self.state['task_id'])
        proof=json.loads(Path(caught.exception.diagnostic_ref['path']).read_text(encoding='utf-8'))
        self.assertTrue(proof['workflow_scope']['task']['task_present'])
        self.assertFalse(proof['lifecycle_scope']['task']['task_present'])
        self.assertFalse(proof['scope_alignment']['lifecycle_engine_is_workflow_engine'])
        self.assertIs(self.workflow.management.engine,other)

    def test_nested_boundary_retains_first_failure_receipt(self):
        with self.assertRaises(ImportBoundaryTaskNotFound) as caught:
            with import_boundary(self.workflow,self.directory,self.context,'outer'):
                with import_boundary(self.workflow,self.directory,self.context,'inner'):
                    raise TaskNotFound('missing')
        proof=json.loads(Path(caught.exception.diagnostic_ref['path']).read_text(encoding='utf-8'))
        self.assertEqual(proof['phase'],'inner')
        self.assertEqual(len(list((self.directory/'evidence').glob('import-boundary-*.json'))),1)

    def test_cancel_and_other_validation_failures_keep_their_existing_type(self):
        for error in (ActionRejected('revoked'),ValueError('invalid source')):
            with self.assertRaises(type(error)) as caught:
                with import_boundary(self.workflow,self.directory,self.context,'prepare_candidate'):
                    raise error
            self.assertIs(caught.exception,error)
        self.assertFalse((self.directory/'evidence').exists())

    def test_diagnostic_write_failure_never_replays_or_masks_original_category(self):
        with patch('shu.import_preparation._publish_import_diagnostic',side_effect=OSError('private detail')):
            with self.assertRaises(ImportBoundaryTaskNotFound) as caught:
                with import_boundary(self.workflow,self.directory,self.context,'prepare_candidate'):
                    raise TaskNotFound('missing')
        self.assertEqual(caught.exception.code,'import_boundary_task_not_found')
        self.assertIsNone(caught.exception.diagnostic_ref)
        self.assertNotIn('private detail',str(caught.exception))


if __name__=='__main__':
    unittest.main()

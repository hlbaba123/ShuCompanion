"""Import evidence growth must not lose a genuinely completed GUI result."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore


class ImportEvidenceBudgetTests(unittest.TestCase):
    def setUp(self):
        folder=TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.root=Path(folder.name)
        self.engine=TaskEngine(self.root); self.addCleanup(self.engine.close)
        self.store=WorkflowStore(self.engine)

    def prepare(self, count):
        state=self.store.submit('只导入单作业，不开始', {'current_deliverable':'maa_import','allow_start':False})
        refs=[]
        for n in range(count-1):
            path=self.root/f"{state['task_id']}-{n}.json"
            path.write_text(json.dumps({'observation':n}),encoding='utf8')
            refs.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        proof=self.root/f"{state['task_id']}-proof.json"
        proof.write_text(json.dumps({'observation_refs':refs,'transport':'screenshot_ocr','start_clicked':False}),encoding='utf8')
        refs.append({'path':str(proof),'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()})
        dispatch=self.store.ensure_dispatch(state['task_id'],'maa','import',{})
        self.store.finish_dispatch(dispatch['dispatch_id'],{'status':'import_verified',
            'battle_start_submitted':False,'receipt_ref':refs[-1],'evidence_refs':refs})
        return state,refs,proof

    def test_refresh_observations_and_bounded_maximum_can_finish_without_another_input(self):
        for count in (34,128):
            with self.subTest(count=count):
                state,refs,_=self.prepare(count)
                result=self.store.complete_import_verified(state['task_id'],refs,expected_epoch=state['epoch'])
                self.assertEqual((result['status'],result['completed_runs']),('completed',0))
                self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM workflow_operations').fetchone()[0],0)

    def test_more_than_128_references_are_still_rejected(self):
        state,refs,_=self.prepare(129)
        with self.assertRaises(ActionRejected):
            self.store.complete_import_verified(state['task_id'],refs,expected_epoch=state['epoch'])
        self.assertNotEqual(self.store.current(state['task_id'])['status'],'completed')

    def test_root_or_child_change_and_revoked_epoch_remain_rejected(self):
        for fault in ('root','child','stop'):
            with self.subTest(fault=fault):
                state,refs,proof=self.prepare(34)
                if fault=='stop':
                    self.store.control(state['task_id'],'stop')
                else:
                    path=proof if fault=='root' else Path(refs[0]['path'])
                    path.write_text('{}',encoding='utf8')
                with self.assertRaises((ActionRejected,ValueError)):
                    self.store.complete_import_verified(state['task_id'],refs,expected_epoch=state['epoch'])
                self.assertNotEqual(self.store.current(state['task_id'])['status'],'completed')

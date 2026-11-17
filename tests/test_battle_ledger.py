"""Real SQLite and real PNG artifacts; no model, window or game inputs."""
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
import zlib

from shu.battle_contract import authorize
from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore
from shu.maa_visual_native import MAA_EXECUTABLE


def png():
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,2,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress(b'\0'+b'\0'*6+b'\0'+b'\0'*6))+chunk(b'IEND',b''))


class BattleLedgerTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name);self.engine=TaskEngine(self.root);self.addCleanup(self.engine.close)
        self.store=WorkflowStore(self.engine)
        self.contract=authorize({'goal':'运行当前已导入单作业',
            'user_request':'开始MAA自动战斗，运行当前已导入的单作业',
            'current_deliverable':'gameplay','task_spec':{'kind':'repeat_stage'}})
        self.state=self.store.submit(self.contract['goal'],self.contract)
        self.tid=self.state['task_id']; self.identity={'hwnd':12,'pid':34,'creation_time':56,'executable':MAA_EXECUTABLE}
        self.dispatch=self.store.ensure_dispatch(self.tid,'maa','battle',{})
        self.store.claim_dispatch(self.dispatch['dispatch_id'],'test')
        self.operation=self.store.prepare_operation(self.tid,self.dispatch['dispatch_id'],'battle:start',
            {'kind':'start','maa_identity':self.identity},starts=True)
        self.store.issue_operation(self.operation['operation_id'])
        self.operation=self.store.enter_submission(self.operation['operation_id'])
        self.base=datetime.fromisoformat(self.operation['submitted_at']).timestamp()
        self.directory=self.root/'task-private'/self.tid/'battle';self.directory.mkdir(parents=True)
        self.running=self.observation('running',self.base+.05,True)
        self.idle1=self.observation('idle1',self.base+.2,False)
        self.idle2=self.observation('idle2',self.base+.4,False)
        self.proof=self.make_proof()

    def ref(self,name,value):
        path=self.directory/(name+'.json');path.write_text(json.dumps(value),encoding='utf-8')
        return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    def read(self,ref):return json.loads(Path(ref['path']).read_text(encoding='utf-8'))

    def observation(self,name,captured,running):
        path=self.directory/(name+'.png');path.write_bytes(png())
        frame={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        return self.ref(name,{'schema':'maa_battle_observation/v1',
            **{k:self.state[k] for k in ('task_id','revision','epoch')},
            'start_operation_id':self.operation['operation_id'],'maa_identity':self.identity,
            'captured_at':captured,'running':running,'idle':not running,
            'screenshot_ref':frame,'image_sha256':frame['sha256'],'method':'win32_printwindow'})

    def make_proof(self,**overrides):
        value={'schema':'maa_battle_idle/v1',**{k:self.state[k] for k in ('task_id','revision','epoch')},
            'start_operation_id':self.operation['operation_id'],'start_payload_hash':self.operation['payload_hash'],
            'battle_start_submitted':True,'external_idle_confirmed':True,'maa_identity':self.identity,
            'observed_running_ref':self.running,'idle_observation_refs':[self.idle1,self.idle2],
            'control_seq':self.store.current(self.tid)['control_seq'],**overrides}
        return self.ref('proof-'+str(time.time_ns()),value)

    def settle(self,ref=None):return self.store.settle_battle_idle(self.operation['operation_id'],ref or self.proof)

    def finish_dispatch(self,**overrides):
        receipt={'status':'battle_finished','start_operation_id':self.operation['operation_id'],
            'battle_start_submitted':True,'external_idle_confirmed':True,
            'evidence_refs':[self.proof],**overrides}
        return self.store.finish_dispatch(self.dispatch['dispatch_id'],receipt)

    def test_running_then_two_idle_settles_only_original_start(self):
        self.store.finish_operation(self.operation['operation_id'],'verified',{'receipt':'input_dispatched'})
        result=self.settle()
        self.assertEqual(result['status'],'verified')
        self.assertTrue(result['result']['battle_idle_verified'])
        self.assertIsNone(self.store.slot_state()['barrier_operation_id'])
        self.assertTrue(self.store.current(self.tid)['writes_enabled'])

    def test_watchdog_pause_cannot_overwrite_a_newer_user_stop(self):
        stopped = self.store.control(self.tid, 'stop')
        with self.assertRaises(ActionRejected):
            self.store.control(self.tid, 'pause', expected_epoch=self.state['epoch'])
        self.assertEqual(self.store.current(self.tid), stopped)
        self.assertEqual(stopped['status'], 'cancelled')

    def test_idempotent_same_proof_but_conflicting_proof_cannot_rewrite(self):
        first=self.settle(); self.assertEqual(self.settle(),first)
        with self.assertRaises((ActionRejected,ValueError)):
            self.settle(self.make_proof(extra='different'))

    def test_unknown_commit_can_settle_without_replaying_start(self):
        self.store.finish_operation(self.operation['operation_id'],'unknown',{'receipt':'input_unknown'})
        self.assertTrue(self.settle()['result']['battle_idle_verified'])
        self.assertEqual(len(self.store.pending_operations()),0)

    def test_idle_only_or_reversed_or_repeated_capture_never_releases(self):
        for change in ({'observed_running_ref':self.idle1}, {'idle_observation_refs':[self.idle2,self.idle1]},
                       {'idle_observation_refs':[self.idle1,self.idle1]},
                       {'idle_observation_refs':[self.idle1,self.observation('too-close',self.base+.21,False)]}):
            with self.subTest(change=change), self.assertRaises((ActionRejected,ValueError)):
                self.settle(self.make_proof(**change))
        self.assertEqual(self.store.slot_state()['barrier_operation_id'],self.operation['operation_id'])

    def test_wrong_identity_operation_payload_or_epoch_rejected(self):
        for change in ({'maa_identity':{**self.identity,'pid':35}}, {'start_operation_id':'other'},
                       {'start_payload_hash':'0'*64}, {'epoch':self.state['epoch']+1},
                       {'task_id':'other'},{'control_seq':self.state['control_seq']+1},
                       {'battle_start_submitted':False},{'external_idle_confirmed':False}):
            with self.subTest(change=change),self.assertRaises((ActionRejected,ValueError)):
                self.settle(self.make_proof(**change))

    def test_changed_image_unknown_method_and_future_capture_rejected(self):
        original=self.read(self.idle2)
        for fields in ({'method':'model_text'}, {'captured_at':time.time()+100},
                       {'image_sha256':'0'*64}, {'idle':False}, {'running':True}):
            changed=self.ref('wrong-'+str(time.time_ns()),{**original,**fields})
            with self.subTest(fields=fields),self.assertRaises((ActionRejected,ValueError)):
                self.settle(self.make_proof(idle_observation_refs=[self.idle1,changed]))
        Path(original['screenshot_ref']['path']).write_bytes(b'not a PNG')
        with self.assertRaises((ActionRejected,ValueError)):self.settle()

    def test_cross_profile_file_or_symlink_rejected(self):
        outside=self.root.parent/('not-owned-'+str(time.time_ns())+'.json')
        outside.write_bytes(Path(self.proof['path']).read_bytes());self.addCleanup(outside.unlink)
        with self.assertRaises((ActionRejected,ValueError)):
            self.settle({'path':str(outside),'sha256':hashlib.sha256(outside.read_bytes()).hexdigest()})

    def test_cancelled_task_settles_only_with_current_stop_control_and_never_resumes(self):
        state=self.store.control(self.tid,'stop')
        with self.assertRaises((ActionRejected,ValueError)):self.settle()
        proof=self.make_proof(control_seq=state['control_seq'])
        self.assertTrue(self.settle(proof)['result']['battle_idle_verified'])
        after=self.store.current(self.tid)
        self.assertEqual(after['status'],'cancelled');self.assertFalse(after['writes_enabled'])
        self.assertEqual(after['epoch'],state['epoch'])

    def test_pause_can_settle_but_resumed_epoch_cannot_take_old_proof(self):
        self.store.control(self.tid,'pause');self.store.control(self.tid,'resume')
        with self.assertRaises((ActionRejected,ValueError)):
            self.settle(self.make_proof())

    def test_verified_idle_and_matching_persistent_maa_result_complete(self):
        self.settle();self.finish_dispatch()
        result=self.store.complete_battle_verified(self.tid,[self.proof],expected_epoch=self.state['epoch'])
        self.assertEqual(result['status'],'completed')
        state=self.store.current(self.tid);self.assertFalse(state['writes_enabled'])
        self.assertEqual(state['control_kind'],'battle_finished')
        self.assertEqual(self.store.complete_battle_verified(self.tid,[self.proof],expected_epoch=self.state['epoch']),result)

    def test_dispatch_prose_or_old_epoch_cannot_complete(self):
        self.finish_dispatch()
        with self.assertRaises((ActionRejected,ValueError)):
            self.store.complete_battle_verified(self.tid,[self.proof],expected_epoch=self.state['epoch'])
        self.settle();self.store.control(self.tid,'pause');self.store.control(self.tid,'resume')
        with self.assertRaises((ActionRejected,ValueError)):
            self.store.complete_battle_verified(self.tid,[self.proof],expected_epoch=self.state['epoch'])

    def test_second_start_remains_quota_limited_after_idle(self):
        self.settle()
        op=self.store.prepare_operation(self.tid,self.dispatch['dispatch_id'],'battle:second',
            {'kind':'start','maa_identity':self.identity},starts=True)
        with self.assertRaises(ActionRejected):self.store.issue_operation(op['operation_id'])


    def test_original_import_contract_cannot_use_battle_settlement(self):
        bad={'goal':'只导入','allow_start':True,'attempt_limit':1,'current_deliverable':'maa_import'}
        from shu.workflow_store import _encode
        with self.engine._transaction():
            encoded,digest=_encode(bad)
            self.engine._db.execute('UPDATE workflow_contracts SET contract=?,payload_hash=? WHERE task_id=?',
                                    (encoded,digest,self.tid))
        with self.assertRaises((ActionRejected,ValueError)):self.settle()

    def test_boolean_epoch_is_not_a_real_epoch(self):
        with self.assertRaises((ActionRejected,ValueError)):
            self.settle(self.make_proof(epoch=True))

    def test_observation_boolean_epoch_and_future_start_time_rejected(self):
        value=self.read(self.running);value['epoch']=True
        fake=self.ref('running-bool',value)
        with self.assertRaises((ActionRejected,ValueError)):
            self.settle(self.make_proof(observed_running_ref=fake))

    def test_unvalidated_legacy_stop_receipt_cannot_clear_new_battle_barrier(self):
        state=self.store.control(self.tid,'stop')
        stop=self.store.prepare_control_operation(self.tid,state['control_seq'],'stop',{'kind':'stop'})
        self.store.issue_operation(stop['operation_id']);self.store.enter_submission(stop['operation_id'])
        self.store.finish_operation(stop['operation_id'],'verified',
            {'stopped':True,'reconciled_operation_id':self.operation['operation_id'],'evidence_ref':self.proof})
        self.assertEqual(self.store.slot_state()['barrier_operation_id'],self.operation['operation_id'])
        self.settle(self.make_proof(control_seq=state['control_seq']))
        self.assertIsNone(self.store.slot_state()['barrier_operation_id'])


class CompoundStartLedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        engine = TaskEngine(self.root)
        self.addCleanup(engine.close)
        self.store = WorkflowStore(engine)
        request = '查找TL-EX-3普通的作业，导入MAA并开始执行'
        self.contract = authorize({'user_request':request, 'current_deliverable':'maa_import',
            'task_spec':{'kind':'import','stage_code':'TL-EX-3'}})
        self.assertEqual(self.contract['workflow_scope'], 'maa_import_then_battle')
        self.task_id = self.store.submit(request,self.contract)['task_id']

    def dispatch(self, step, import_dispatch_id=None):
        dispatch = self.store.ensure_dispatch(self.task_id,'maa',step,
            {'contract': self.contract, 'execution_step':step, 'import_dispatch_id':import_dispatch_id})
        self.store.claim_dispatch(dispatch['dispatch_id'],'test')
        return dispatch['dispatch_id']

    def imported_job(self):
        import_dispatch_id = self.dispatch('import')
        op = self.store.prepare_operation(self.task_id, import_dispatch_id, 'code',
            {'kind':'maa_import','action':'replace_code','code':'prts://123'})
        self.store.issue_operation(op['operation_id'])
        self.store.enter_submission(op['operation_id'])
        self.store.finish_operation(op['operation_id'],'verified',{'input_dispatched':True})
        from shu.maa_queue_receipt import _publish
        proof = _publish(self.root, 'import-proof', {'transport':'screenshot_ocr','start_clicked':False,
            'code':'prts://123','actions':[{'operation_id':op['operation_id'],'action':'replace_code'}]})
        self.store.finish_dispatch(import_dispatch_id, {'status':'import_verified', 'transport':'screenshot_keyboard',
            'battle_start_submitted':False,'mystery_code':'prts://123','receipt_ref':proof,'evidence_refs':[proof]})
        return import_dispatch_id, proof

    def start(self, dispatch_id, import_dispatch_id, proof):
        return self.store.prepare_operation(self.task_id, dispatch_id, 'compound:'+dispatch_id,
            {'kind':'start','target':'battle_start','import_dispatch_id':import_dispatch_id,
             'import_proof_ref':proof},starts=True)

    def test_compound_start_requires_current_verified_import_in_ledger(self):
        for step in ('import','battle'):
            dispatch_id = self.dispatch(step)
            with self.subTest(step=step),self.assertRaises(ActionRejected):
                self.start(dispatch_id,None,None)

    def test_current_durable_import_can_back_one_start_submission(self):
        import_dispatch_id, proof = self.imported_job()
        battle = self.dispatch('battle', import_dispatch_id)
        start = self.start(battle, import_dispatch_id, proof)
        self.store.issue_operation(start['operation_id'])
        self.assertIsNotNone(self.store.enter_submission(start['operation_id'])['submitted_at'])
        with self.assertRaises(ActionRejected):
            self.store.require_start_attempt_available(self.task_id)

    def test_changed_import_proof_closes_submission_gate_after_issue(self):
        import_dispatch_id, proof = self.imported_job()
        battle = self.dispatch('battle', import_dispatch_id)
        start = self.start(battle, import_dispatch_id, proof)
        self.store.issue_operation(start['operation_id'])
        path = Path(proof['path'])
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(start['operation_id'])
        self.assertIsNone(self.store.get_operation(start['operation_id'])['submitted_at'])

if __name__=='__main__':unittest.main()

"""Legacy focus recovery: real SQLite/PNG evidence, zero native GUI input."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from contextlib import closing
from unittest.mock import MagicMock, patch

from shu.battle_contract import authorize
from shu.core import TaskEngine
from shu.maa_focus_recovery import reconcile_known_focus_failure
from shu.maa_queue_receipt import _publish
from shu.workflow_store import WorkflowStore
from tests.test_battle_ledger import png


MAA={'hwnd':12,'pid':34,'creation_time':56,'executable':r'D:\MAA\MAA-v6.21.4-win-x64\MAA.exe'}
GAME={'hwnd':13,'pid':35,'creation_time':57,'executable':r'D:\Arknights\Arknights.exe'}


class FocusRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.contract=authorize({'goal':'运行当前单作业','user_request':'开始MAA自动战斗一次',
            'current_deliverable':'gameplay','task_spec':{'kind':'repeat_stage'}})
        self.prior=self.root/'prior';engine=TaskEngine(self.prior);old=WorkflowStore(engine)
        state=old.submit('prior',self.contract);self.old_tid=state['task_id']
        dispatch=old.ensure_dispatch(self.old_tid,'maa','focus',{})
        old.claim_dispatch(dispatch['dispatch_id'],'test')
        operation=old.prepare_operation(self.old_tid,dispatch['dispatch_id'],'focus',
            {'kind':'battle_navigation','action':'focus','surface':'game','maa_identity':MAA,'game_identity':GAME})
        self.old_id=operation['operation_id'];old.issue_operation(self.old_id);old.enter_submission(self.old_id)
        self.old_proof={'operation_id':self.old_id,'args':{'action':'focus','surface':'game'},
            'response':{'status':'unknown','error':'focus_denied','helper_exit_confirmed':True,
                        'submission_entered':True}}
        self.old_ref=_publish(self.prior/'task-private'/self.old_tid,'native-input',self.old_proof)
        old.finish_operation(self.old_id,'unknown',{'input_dispatched':False,'evidence_ref':self.old_ref})
        old.control(self.old_tid,'pause');engine.close()
        self.before={p.relative_to(self.prior):p.read_bytes() for p in self.prior.rglob('*') if p.is_file()}
        self.data=self.root/'current';self.engine=TaskEngine(self.data);self.addCleanup(self.engine.close)
        self.store=WorkflowStore(self.engine);self.context=self.store.submit('current',self.contract)
        self.statepath=self.root/'desktop.lock.state.json'
        self.workerstate={'unknown':True,'revoked':True,'reason':'submission_unconfirmed','updated_at':123.25}
        self.statepath.write_text(json.dumps(self.workerstate),encoding='utf-8')
        self.pin=hashlib.sha256(self.statepath.read_bytes()).hexdigest()
        self.health={'unknown':True,'revoked':True,'reason':'submission_unconfirmed','helper_exit_confirmed':True,
            'child_alive':False,'pending':0,'stop_pending':0,'closed':False}
        self.requests=[];self.reconciled=[];self.guard=threading.Event();self.snapshot_hook=None
        self.supervisor=SimpleNamespace(target_identity=MAA,directory=str(self.data),_state_path=self.statepath,
            _gate=threading.RLock(),_current=None,_own_slot=lambda:None,
            health=lambda:copy.deepcopy(self.health),request=self.request,reconcile=self.reconcile)

    def request(self,command,**kwargs):
        self.requests.append(copy.deepcopy(command));surface=command['args']['surface']
        identity=MAA if surface=='maa' else GAME
        path=self.data/(surface+'.png');path.write_bytes(png())
        frame={'window':{**identity,'client_rect':{'width':2,'height':2}},'method':'win32_printwindow',
            'image_path':str(path),'image_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'screenshot_id':surface}
        if self.snapshot_hook:self.snapshot_hook(surface,frame)
        return {'status':'completed','result':frame}

    def reconcile(self,evidence,**kwargs):self.reconciled.append((evidence,kwargs))

    def recover(self,**kwargs):
        return reconcile_known_focus_failure(self.store,self.supervisor,context=self.context,
            guard=lambda:None,prior_profile=self.prior,game_identity=GAME,**kwargs)

    def test_legacy_reason_without_explicit_state_binding_remains_blocked(self):
        result=self.recover()
        self.assertEqual(result['status'],'blocked',result)
        self.assertFalse(self.reconciled)

    def test_exact_pinned_focus_failure_recovers_readonly_and_retains_history(self):
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'reconciled',result)
        self.assertEqual(result['gui_inputs'],0)
        self.assertEqual([v['op'] for v in self.requests],['snapshot','snapshot'])
        self.assertEqual(len(self.reconciled),1)
        proof=json.loads(Path(result['evidence_refs'][0]['path']).read_text(encoding='utf-8'))
        self.assertEqual(proof['trusted_legacy_fence_binding']['state_sha256'],self.pin)
        self.assertEqual(proof['source']['source_business_outcome'],'unknown')
        for relative,raw in self.before.items():self.assertEqual((self.prior/relative).read_bytes(),raw)
        self.assertEqual(set(self.before),{p.relative_to(self.prior) for p in self.prior.rglob('*') if p.is_file()})
        self.assertEqual(self.store.pending_operations(),[])

    def test_unrelated_later_fence_is_not_cleared_by_valid_older_focus_receipt(self):
        self.statepath.write_text(json.dumps({**self.workerstate,'updated_at':124}),encoding='utf-8')
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked',result);self.assertFalse(self.reconciled)

    def test_state_change_during_observation_is_rejected(self):
        def change(surface,frame):
            if surface=='game':self.statepath.write_text(json.dumps({**self.workerstate,'updated_at':125}),encoding='utf-8')
        self.snapshot_hook=change
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked',result);self.assertFalse(self.reconciled)

    def test_wrong_binding_and_non_focus_error_cannot_recover(self):
        import sqlite3
        with closing(sqlite3.connect(self.prior/'tasks.sqlite3')) as db:
            proof=copy.deepcopy(self.old_proof);proof['response']['error']='partial_mouse_input'
            ref=_publish(self.prior/'task-private'/self.old_tid,'changed-input',proof)
            db.execute('UPDATE workflow_operations SET result=? WHERE operation_id=?',
                (json.dumps({'input_dispatched':False,'evidence_ref':ref}),self.old_id))
            db.commit()
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked',result);self.assertFalse(self.reconciled)

    def test_changed_png_identity_or_corrupt_pixels_never_clear(self):
        def damage(surface,frame):
            if surface=='game':
                path=Path(frame['image_path']);path.write_bytes(path.read_bytes()[:24])
                frame['image_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        self.snapshot_hook=damage
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked',result);self.assertFalse(self.reconciled)

    def test_cancel_or_pending_current_input_never_reopens(self):
        cancel=threading.Event();cancel.set()
        result=self.recover(expected_worker_state_sha256=self.pin,cancel_event=cancel)
        self.assertEqual(result['status'],'blocked');self.assertFalse(self.reconciled)

    def test_current_profile_pending_input_or_past_battle_start_cannot_be_cleared(self):
        dispatch=self.store.ensure_dispatch(self.context['task_id'],'maa','new',{})
        self.store.claim_dispatch(dispatch['dispatch_id'],'test')
        self.store.prepare_operation(self.context['task_id'],dispatch['dispatch_id'],'new-pending',
            {'kind':'battle_navigation','action':'focus','surface':'game','maa_identity':MAA,'game_identity':GAME})
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked');self.assertFalse(self.reconciled)
        self.assertFalse(self.requests)

    def test_old_profile_with_submitted_start_never_uses_focus_recovery(self):
        import sqlite3
        with closing(sqlite3.connect(self.prior/'tasks.sqlite3')) as db:
            db.execute('UPDATE workflow_operations SET starts=1 WHERE operation_id=?',(self.old_id,));db.commit()
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked');self.assertFalse(self.reconciled)

    def test_stop_or_revocation_during_snapshot_cannot_resume(self):
        def pause(surface,frame):
            if surface=='game':self.store.control(self.context['task_id'],'pause')
        self.snapshot_hook=pause
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked');self.assertFalse(self.reconciled)

    def test_restarted_process_requires_explicit_retirement_proof_for_old_identity(self):
        current={**MAA,'pid':100,'creation_time':1000}
        self.supervisor.target_identity=current
        original_request=self.request
        def snapshot(command,**kwargs):
            reply=original_request(command,**kwargs)
            if command['args']['surface']=='maa':reply['result']['window'].update(current)
            return reply
        self.supervisor.request=snapshot
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked');self.assertFalse(self.reconciled)
        retired=[]
        def retired_identity(identity):retired.append(identity);return identity==MAA
        result=self.recover(expected_worker_state_sha256=self.pin,retired_identity=retired_identity)
        self.assertEqual(result['status'],'reconciled',result)
        self.assertTrue(retired);self.assertTrue(all(v==MAA for v in retired))

    def test_uncheckpointed_source_journal_blocks_immutable_read(self):
        (self.prior/'tasks.sqlite3-wal').write_bytes(b'active journal')
        result=self.recover(expected_worker_state_sha256=self.pin)
        self.assertEqual(result['status'],'blocked');self.assertFalse(self.reconciled)


class RetiredIdentityTests(unittest.TestCase):
    def call(self,kernel,error=0):
        from shu.maa_focus_recovery import process_identity_retired
        backend=SimpleNamespace(native=SimpleNamespace(kernel=kernel))
        with patch('shu.maa_focus_recovery.ctypes.get_last_error',return_value=error):
            return process_identity_retired(MAA,native_backend=backend)

    def kernel(self,*,handle=1,creation=56,exit_code=259):
        kernel=MagicMock();kernel.OpenProcess.return_value=handle
        def times(handle,created,*others):
            created._obj.dwHighDateTime=creation>>32;created._obj.dwLowDateTime=creation&0xffffffff
            return True
        def exited(handle,code):code._obj.value=exit_code;return True
        kernel.GetProcessTimes.side_effect=times;kernel.GetExitCodeProcess.side_effect=exited
        return kernel

    def test_only_invalid_pid_is_a_conclusive_open_failure(self):
        for error,expected in ((87,True),(5,False),(0,False),(299,False)):
            with self.subTest(error=error):
                kernel=self.kernel(handle=0)
                self.assertIs(self.call(kernel,error),expected)
                kernel.CloseHandle.assert_not_called()

    def test_live_matching_creation_is_never_retired(self):
        kernel=self.kernel();self.assertFalse(self.call(kernel));kernel.CloseHandle.assert_called_once_with(1)

    def test_verified_exit_or_pid_reuse_is_retired(self):
        for change in ({'creation':57},{'exit_code':0}):
            with self.subTest(change=change):
                kernel=self.kernel(**change);self.assertTrue(self.call(kernel));kernel.CloseHandle.assert_called_once()

    def test_unreadable_times_and_errors_are_not_retirement(self):
        kernel=self.kernel();kernel.GetProcessTimes.side_effect=OSError('access denied')
        self.assertFalse(self.call(kernel));kernel.CloseHandle.assert_called_once()
        kernel=self.kernel();kernel.GetProcessTimes.side_effect=None;kernel.GetProcessTimes.return_value=False
        self.assertFalse(self.call(kernel));kernel.CloseHandle.assert_called_once()


if __name__=='__main__':unittest.main()

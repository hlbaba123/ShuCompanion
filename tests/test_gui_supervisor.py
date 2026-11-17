"""Real spawned helpers; no Windows GUI, model or game access."""
from pathlib import Path
import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from shu.gui_supervisor import GuiSupervisor, _GateRejected, _MaaVisualHandler


IDENTITY = {"hwnd": 41, "pid": 42, "creation_time": 43}


class QueueVisualIpcTests(unittest.TestCase):
    def setUp(self):
        self.handler = _MaaVisualHandler.__new__(_MaaVisualHandler)
        self.handler.adapter = Mock()
        self.gate = Mock()

    def dispatch(self, **args):
        return self.handler({'op':'act', 'args':{'x':10,'y':20,'screenshot_id':'fresh',**args}}, self.gate)

    def test_queue_right_click_only_reaches_add(self):
        self.dispatch(action='click',button='right',target='queue_add')
        self.handler.adapter.click.assert_called_once_with(10,20,'fresh',button='right',
            target='queue_add',guard=self.gate)
        for target in ('start','stop','row_delete:one','queue_clear',None):
            with self.subTest(target=target), self.assertRaises(_GateRejected):
                self.dispatch(action='click',button='right',target=target)
        self.assertEqual(self.handler.adapter.click.call_count,1)

    def test_queue_scroll_is_bounded_and_only_for_list(self):
        self.dispatch(action='scroll_queue',target='queue_list',notches=-2)
        self.handler.adapter.scroll_queue.assert_called_once_with(10,20,-2,'fresh',
            target='queue_list',guard=self.gate)
        for target,notches in [('queue_add',1),('queue_list',0),('queue_list',True),('queue_list',4)]:
            with self.subTest(target=target,notches=notches), self.assertRaises(_GateRejected):
                self.dispatch(action='scroll_queue',target=target,notches=notches)
        self.assertEqual(self.handler.adapter.scroll_queue.call_count,1)

    def test_legacy_left_click_retains_adapter_call_and_denies_global_controls(self):
        self.dispatch(action='click',target='code_input')
        self.handler.adapter.click.assert_called_once_with(10,20,'fresh',guard=self.gate)
        for target in ('start','stop','queue_clear','queue_clear_unchecked'):
            with self.subTest(target=target), self.assertRaises(_GateRejected):
                self.dispatch(action='click',target=target)
        self.assertEqual(self.handler.adapter.click.call_count,1)


def synthetic_gui(command, enter_submission):
    """Only the external GUI boundary is replaced; IPC/processes remain real."""
    args = command.get("args", {})
    marker = args.get("marker")
    if marker:
        Path(marker).write_text("started", encoding="utf-8")
    if args.get("hang"):
        while True:
            time.sleep(1)
    if args.get("wait_for"):
        while not Path(args["wait_for"]).exists():
            time.sleep(.005)
    if command["op"] == "act":
        enter_submission()
        if args.get("effect"):
            Path(args["effect"]).write_text("external input", encoding="utf-8")
        if args.get("hang_after"):
            while True:
                time.sleep(1)
        if args.get("hold_after"):
            while not Path(args["hold_after"]).exists():
                time.sleep(.01)
    if args.get("thread_timeout"):
        error = RuntimeError("UIA thread did not exit")
        error.code = "uia_timeout"
        raise error
    return {"echo": args.get("value"), "identity": IDENTITY}


def synthetic_drag(command,enter_submission):
    """File stands in for button state; only IPC/spawn/cancellation are real."""
    if command['op']=='snapshot':return {'ready':True}
    args=command['args'];button=Path(args['button']);marker=Path(args['marker'])
    if args.get('wait_before'):
        marker.write_text('before',encoding='utf-8')
        while True:time.sleep(.02)
    enter_submission()
    button.write_text('down',encoding='utf-8');marker.write_text('down',encoding='utf-8')
    try:
        if args.get('complete'):return {'drag':'done'}
        while True:
            if not args.get('hang_after'):enter_submission()
            time.sleep(.008)
    finally:
        button.write_text('up',encoding='utf-8')
        (button.parent/'child-finally').write_text('released',encoding='utf-8')


class FileDragButton:
    def __init__(self,path):
        self.path=path;self.releases=[];self.owner=None;self.fail=False
        self.path.write_text('up',encoding='utf-8')
    def is_down(self):return self.path.read_text(encoding='utf-8')=='down'
    def release_after_exit(self):
        health=self.owner.health()
        self.releases.append({'confirmed':health['helper_exit_confirmed'],'alive':health['child_alive'],
                              'at':time.time()})
        if not self.fail:self.path.write_text('up',encoding='utf-8')
        return {'button_released':not self.fail,'release_only':True,'input_sent':not self.fail}


class ForegroundDragCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.owners=[]
    def tearDown(self):
        for owner,button in self.owners:
            button.fail=False
            owner.close()
        self.temp.cleanup()
    def setup_owner(self,mode='foreground'):
        folder=self.root/str(len(self.owners));folder.mkdir()
        button=FileDragButton(folder/'button')
        owner=GuiSupervisor(IDENTITY,backend='battle',navigation_mode=mode,
            navigation_identity={'hwnd':10,'pid':20,'creation_time':30,'executable':r'D:\Arknights.exe'},
            directory=folder,lock_path=folder/'desktop.lock',test_handler=synthetic_drag,drag_button=button)
        button.owner=owner;self.owners.append((owner,button));return owner,button,folder
    def run_drag(self,owner,button,folder,*,timeout=5,**extra):
        cancel=threading.Event();results=[];marker=folder/'marker'
        command={'op':'act','args':{'surface':'game','action':'drag','button':str(button.path),
                    'marker':str(marker),**extra}}
        thread=threading.Thread(target=lambda:results.append(owner.request(command,timeout=timeout,cancel_event=cancel)))
        thread.start();wait_for_file(marker)
        return cancel,results,thread
    def test_cooperative_cancel_reaches_child_finally_before_retirement(self):
        owner,button,folder=self.setup_owner();cancel,result,thread=self.run_drag(owner,button,folder)
        cancel.set();thread.join(3)
        self.assertFalse(thread.is_alive());self.assertEqual(result[0]['status'],'unknown')
        self.assertTrue((folder/'child-finally').exists())
        self.assertFalse(button.is_down());self.assertFalse(owner.health()['pointer_cleanup_pending'])
        self.assertEqual(button.releases,[])
        self.assertTrue(owner.health()['unknown']);self.assertTrue(owner.health()['revoked'])

    def test_hung_admitted_drag_cancel_or_timeout_releases_only_after_confirmed_exit(self):
        for timeout in (False,True):
            with self.subTest(timeout=timeout):
                owner,button,folder=self.setup_owner()
                if timeout:
                    # This case exercises a deadline AFTER drag admission,
                    # not multiprocessing's cold-import/child startup limit.
                    ready=owner.request({'op':'snapshot','args':{}},timeout=10)
                    self.assertEqual(ready['status'],'completed',ready)
                cancel,result,thread=self.run_drag(owner,button,folder,timeout=1 if timeout else 5,hang_after=True)
                if not timeout:cancel.set()
                thread.join(4)
                self.assertFalse(thread.is_alive());self.assertEqual(result[0]['status'],'unknown')
                self.assertTrue(result[0]['submission_entered'])
                self.assertFalse((folder/'child-finally').exists())
                self.assertFalse(button.is_down());self.assertEqual(len(button.releases),1)
                self.assertTrue(button.releases[0]['confirmed']);self.assertFalse(button.releases[0]['alive'])
                self.assertGreaterEqual(button.releases[0]['at'],owner.health()['timestamps']['helper_exit_confirmed'])
                self.assertTrue(owner.health()['unknown'])
                self.assertEqual(owner.request({'op':'act','args':{}},timeout=1)['status'],'rejected')

    def test_no_unsubmitted_background_maa_or_click_scope_releases(self):
        for mode,args in (('foreground',{'wait_before':True}),('background',{'hang_after':True}),
                          ('foreground',{'hang_after':True,'surface':'maa'}),
                          ('foreground',{'hang_after':True,'action':'click'})):
            with self.subTest(mode=mode,args=args):
                owner,button,folder=self.setup_owner(mode)
                cancel,result,thread=self.run_drag(owner,button,folder,**args)
                cancel.set();thread.join(3)
                self.assertFalse(thread.is_alive());self.assertEqual(button.releases,[])
                self.assertFalse(owner.health()['pointer_cleanup_pending'])
                button.path.write_text('up',encoding='utf-8')

    def test_unconfirmed_exit_never_releases_or_allows_handover(self):
        owner,button,folder=self.setup_owner();cancel,result,thread=self.run_drag(owner,button,folder,hang_after=True)
        process=owner._child
        with patch.object(process,'terminate',side_effect=OSError('denied')):
            cancel.set();thread.join(3)
            self.assertFalse(thread.is_alive());self.assertEqual(result[0]['status'],'unknown')
            self.assertFalse(result[0]['helper_exit_confirmed']);self.assertEqual(button.releases,[])
            self.assertTrue(button.is_down());self.assertTrue(owner.health()['pointer_cleanup_pending'])
            with self.assertRaisesRegex(RuntimeError,'helper_exit_unconfirmed'):owner.close()

    def test_failed_release_preserves_durable_pending_and_reconcile_cannot_bypass(self):
        owner,button,folder=self.setup_owner();button.fail=True
        cancel,result,thread=self.run_drag(owner,button,folder,hang_after=True)
        cancel.set();thread.join(3)
        self.assertEqual(result[0]['status'],'unknown');self.assertTrue(button.is_down())
        self.assertTrue(owner.health()['pointer_cleanup_pending'])
        self.assertTrue(json.loads(owner._state_path.read_text(encoding='utf-8'))['pointer_cleanup_pending'])
        with self.assertRaisesRegex(RuntimeError,'pointer_release_unconfirmed'):
            owner.reconcile({'reconciled':True,'evidence_ref':'arbitrary'},resume=True)
        with self.assertRaisesRegex(RuntimeError,'pointer_release_unconfirmed'):owner.close()
        self.assertIsNotNone(owner._slot);self.assertFalse(owner._slot.closed)

    def test_already_held_user_button_is_rejected_before_admission(self):
        owner,button,folder=self.setup_owner();button.path.write_text('down',encoding='utf-8')
        result=owner.request({'op':'act','args':{'surface':'game','action':'drag','button':str(button.path),
            'marker':str(folder/'marker')}},timeout=2)
        self.assertFalse(result['submission_entered']);self.assertFalse((folder/'marker').exists())
        self.assertEqual(button.releases,[]);button.path.write_text('up',encoding='utf-8')

    def test_completed_drag_verifies_button_up_and_leaves_old_state_shape(self):
        owner,button,folder=self.setup_owner()
        cancel,result,thread=self.run_drag(owner,button,folder,complete=True);thread.join(3)
        self.assertEqual(result[0]['status'],'completed');self.assertFalse(button.is_down())
        self.assertFalse(owner.health()['pointer_cleanup_pending']);self.assertFalse(owner.health()['unknown'])
        self.assertEqual(set(json.loads(owner._state_path.read_text(encoding='utf-8'))),
                         {'unknown','revoked','reason','updated_at'})
        self.assertEqual(button.releases,[])

    def test_failed_cleanup_persistence_remains_owned_for_a_safe_close_retry(self):
        owner,button,folder=self.setup_owner()
        cancel,result,thread=self.run_drag(owner,button,folder,hang_after=True)
        with patch('shu.gui_supervisor.os.fsync',side_effect=OSError('disk failure')):
            cancel.set();thread.join(3)
            self.assertTrue(owner.health()['pointer_cleanup_pending'])
        owner.close()
        self.assertFalse(owner.health()['pointer_cleanup_pending'])

    def test_restart_cannot_use_generic_reconcile_to_forget_pending_drag_cleanup(self):
        owner,button,folder=self.setup_owner()
        owner._state_path.write_text(json.dumps({'unknown':True,'revoked':True,'reason':'pointer_release_unconfirmed',
            'pointer_cleanup_pending':True,'pointer_cleanup_request_id':'a'*32,'updated_at':1}),encoding='utf-8')
        owner._read_state()
        self.assertTrue(owner.health()['pointer_cleanup_pending'])
        self.assertEqual(owner._pointer_cleanup_request_id,'a'*32)
        with self.assertRaisesRegex(RuntimeError,'pointer_release_unconfirmed'):
            owner.reconcile({'reconciled':True,'evidence_ref':'arbitrary'},resume=True)
        self.assertEqual(button.releases,[])
        # This is an injected pending-state fixture, no actual button was held.
        # Leave production recovery closed, then restore only the test owner.
        owner._pointer_cleanup_pending=False


class OfflineMaaHandler:
    def __init__(self):
        self.handler = None

    def __call__(self, command, enter_submission):
        from tests.test_maa_uia import FakeProvider
        from shu.gui_supervisor import _MaaHandler
        if self.handler is None:
            provider = FakeProvider()
            provider.nodes[1]["name"] = "停止"
            with patch("shu.maa_uia.NativeUiaBackend", return_value=provider):
                self.handler = _MaaHandler({"hwnd": 101, "pid": 202, "creation_time": 777})
        return self.handler(command, enter_submission)


def orphan_parent(lock_path, marker, release):
    supervisor = GuiSupervisor(IDENTITY, lock_path=lock_path, test_handler=synthetic_gui)
    supervisor.request({"op": "act", "args": {"effect": marker, "hold_after": release}}, timeout=30)


def wait_for_file(path, seconds=3):
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    if not path.exists():
        raise AssertionError("spawned helper did not reach test boundary")


class GuiSupervisorTests(unittest.TestCase):
    def test_readonly_snapshot_preserves_exact_persisted_unknown_fence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'lock';state=path.with_suffix('.state.json')
            original=b'{"unknown":true,"revoked":true,"reason":"submission_unconfirmed","updated_at":123.5}'
            state.write_bytes(original)
            owner=GuiSupervisor(IDENTITY,lock_path=path,test_handler=synthetic_gui)
            try:
                result=owner.request({'op':'snapshot','args':{}},timeout=3)
                self.assertEqual(result['status'],'completed')
                self.assertEqual(state.read_bytes(),original)
                self.assertTrue(owner.health()['unknown'])
            finally:owner.close()

    def test_only_pre_admission_start_failure_can_drop_legacy_unknown_flag(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'lock'
            state=path.with_suffix('.state.json')
            for reason,unknown in [('helper_exited_during_start',False),('submission_unconfirmed',True),('helper_unresponsive',True)]:
                state.write_text(json.dumps({'unknown':True,'revoked':True,'reason':reason}),encoding='utf-8')
                owner=GuiSupervisor(IDENTITY,lock_path=path)
                try:
                    self.assertEqual(owner.health()['unknown'],unknown)
                    self.assertTrue(owner.health()['revoked'])
                finally:owner.close()
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.supervisors = []

    def tearDown(self):
        for supervisor in self.supervisors:
            supervisor.close()
        self.tmp.cleanup()

    def supervisor(self, **kwargs):
        supervisor = GuiSupervisor(IDENTITY, lock_path=self.directory / "desktop.lock",
                                   test_handler=synthetic_gui, **kwargs)
        self.supervisors.append(supervisor)
        return supervisor

    def test_spawned_json_request_and_final_gate_before_effect(self):
        supervisor = self.supervisor()
        effect = self.directory / "effect"
        seen = []
        result = supervisor.request({"op": "act", "args": {"effect": str(effect), "value": "黍"}},
                                    timeout=3, admit_callback=lambda: seen.append(effect.exists()))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen, [False])
        self.assertEqual(result["result"]["echo"], "黍")
        self.assertTrue(effect.exists())
        self.assertTrue(result["submission_entered"])

    def test_same_global_state_persists_when_atomic_replace_is_unavailable(self):
        supervisor = self.supervisor()
        effect = self.directory / 'input-without-rename'
        with patch('shu.gui_supervisor.os.replace', side_effect=OSError(6, 'invalid handle')):
            result = supervisor.request({'op': 'act', 'args': {'effect': str(effect)}}, timeout=3)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['submission_entered'])
        self.assertTrue(effect.exists())
        state = self.directory / 'desktop.lock.state.json'
        self.assertEqual(supervisor._state_path, state)
        self.assertFalse(json.loads(state.read_text(encoding='utf-8'))['unknown'])
        self.assertEqual(list(self.directory.glob('desktop.lock.state.json.*.tmp')), [])

    def test_state_cannot_be_written_without_owning_the_global_slot(self):
        owner = self.supervisor()
        self.assertEqual(owner.request({'op': 'snapshot'}, timeout=3)['status'], 'completed')
        owner._unknown = owner._revoked = True
        owner._persist()
        state_path = self.directory / 'desktop.lock.state.json'
        before = state_path.read_bytes()
        other = self.supervisor()
        other._unknown = other._revoked = False
        with self.assertRaisesRegex(RuntimeError, 'desktop_control_slot_not_owned'):
            other._persist()
        self.assertEqual(state_path.read_bytes(), before)

    def test_empty_or_torn_global_state_closes_input_after_restart(self):
        state_path = self.directory / 'desktop.lock.state.json'
        for content in ('', '{"unknown":true', '{"unknown":false,"revoked":'):
            with self.subTest(content=content):
                state_path.write_text(content, encoding='utf-8')
                supervisor = self.supervisor()
                self.assertTrue(supervisor.health()['unknown'])
                self.assertTrue(supervisor.health()['revoked'])
                self.assertEqual(supervisor.health()['reason'], 'control_state_unreadable')
                self.assertEqual(supervisor.request({'op': 'act', 'args': {}}, timeout=1)['status'], 'rejected')
                supervisor.close()

    def test_fsync_failure_never_admits_external_input(self):
        supervisor = self.supervisor()
        effect = self.directory / 'forbidden-input'
        with patch('shu.gui_supervisor.os.fsync', side_effect=PermissionError(13, 'permission denied')):
            result = supervisor.request({'op': 'act', 'args': {'effect': str(effect)}}, timeout=3)
        self.assertEqual(result['status'], 'not_issued', result)
        self.assertFalse(result['submission_entered'])
        self.assertFalse(effect.exists())

    def test_lock_open_failure_returns_unknown_without_writing_unowned_state(self):
        supervisor = self.supervisor()
        results = []
        thread = threading.Thread(target=lambda: results.append(supervisor.request(
            {'op': 'snapshot'}, timeout=1)), daemon=True)
        with patch('shu.gui_supervisor._open_slot', side_effect=PermissionError(13, 'permission denied')):
            thread.start()
            thread.join(2)
        self.assertFalse(thread.is_alive(), 'a persistence rejection must still finish the request')
        self.assertEqual(results[0]['status'], 'unknown')
        self.assertFalse((self.directory / 'desktop.lock.state.json').exists())

    def test_inflight_state_is_fsynced_before_input_and_receipt_clears_it(self):
        import os
        supervisor = self.supervisor()
        effect = self.directory / 'durability-order'
        original_fsync, observed = os.fsync, []
        def sync_then_record(descriptor):
            original_fsync(descriptor)
            observed.append(effect.exists())
        with patch('shu.gui_supervisor.os.fsync', side_effect=sync_then_record):
            result = supervisor.request({'op': 'act', 'args': {'effect': str(effect)}}, timeout=3)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(observed, [False, True])

    def test_stop_revokes_hanging_process_without_rpc_wait_and_no_false_stop(self):
        supervisor = self.supervisor()
        marker = self.directory / "hung"
        results = []
        thread = threading.Thread(target=lambda: results.append(supervisor.request(
            {"op": "snapshot", "args": {"marker": str(marker), "hang": True}}, timeout=15)))
        thread.start()
        wait_for_file(marker)
        revoked = threading.Event()
        started = time.monotonic()
        stop = supervisor.request_stop(revoke_callback=revoked.set)
        self.assertLess(time.monotonic() - started, .3)
        self.assertTrue(revoked.is_set())
        self.assertTrue(supervisor.health()["revoked"])
        self.assertEqual(stop["status"], "stopping")
        self.assertFalse(stop["external_stop_confirmed"])
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(results[0]["status"], "completed")

    def test_issued_then_stop_before_final_gate_cannot_send_input(self):
        supervisor = self.supervisor()
        marker, release, effect = [self.directory / name for name in ("issued", "release", "effect")]
        result = []
        admissions = []
        thread = threading.Thread(target=lambda: result.append(supervisor.request(
            {"op": "act", "args": {"marker": str(marker), "wait_for": str(release), "effect": str(effect)}},
            timeout=4, admit_callback=lambda: admissions.append("entered"))))
        thread.start()
        wait_for_file(marker)
        supervisor.request_stop()
        release.touch()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(effect.exists())
        self.assertEqual(admissions, [])
        self.assertEqual(result[0]["status"], "not_issued")

    def test_final_gate_rejection_is_not_issued(self):
        supervisor = self.supervisor()
        effect = self.directory / "effect"
        result = supervisor.request({"op": "act", "args": {"effect": str(effect)}}, timeout=3,
                                    admit_callback=lambda: False)
        self.assertEqual(result["status"], "not_issued")
        self.assertFalse(effect.exists())

    def test_hard_deadline_kills_and_joins_before_new_helper_unknown_survives_restart(self):
        supervisor = self.supervisor()
        result = supervisor.request({"op": "act", "args": {"hang_after": True}}, timeout=.8)
        self.assertEqual(result["status"], "unknown")
        health = supervisor.health()
        self.assertTrue(health["unknown"])
        self.assertTrue(health["helper_exit_confirmed"])
        self.assertFalse(health["child_alive"])
        self.assertEqual(supervisor.request({"op": "act", "args": {}}, timeout=2)["status"], "rejected")
        supervisor.close()
        reopened = self.supervisor()
        self.assertTrue(reopened.health()["unknown"])
        self.assertEqual(reopened.request({"op": "act", "args": {}}, timeout=2)["status"], "rejected")
        self.assertEqual(reopened.request({"op": "snapshot"}, timeout=3)["status"], "completed")
        reopened.reconcile({"reconciled": True, "evidence_ref": "readback-1"}, resume=True)
        self.assertEqual(reopened.request({"op": "act", "args": {}}, timeout=3)["status"], "completed")

    def test_thread_timeout_is_process_unknown_and_old_helper_is_gone(self):
        supervisor = self.supervisor()
        result = supervisor.request({"op": "snapshot", "args": {"thread_timeout": True}}, timeout=3)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(supervisor.health()["helper_exit_confirmed"])
        self.assertFalse(supervisor.health()["child_alive"])

    def test_same_desktop_lock_excludes_second_supervisor(self):
        first, second = self.supervisor(), self.supervisor()
        self.assertEqual(first.request({"op": "snapshot"}, timeout=3)["status"], "completed")
        self.assertEqual(second.request({"op": "snapshot"}, timeout=1)["status"], "rejected")
        first.close()
        self.assertEqual(second.request({"op": "snapshot"}, timeout=3)["status"], "completed")

    def test_stop_clears_waiting_ordinary_work_and_uses_fresh_cancel_context(self):
        supervisor = self.supervisor()
        marker = self.directory / "active"
        running = threading.Thread(target=lambda: supervisor.request(
            {"op": "snapshot", "args": {"marker": str(marker), "hang": True}}, timeout=8))
        running.start()
        wait_for_file(marker)
        effects, queued = [], []
        for index in range(3):
            effect = self.directory / f"queued-{index}"
            effects.append(effect)
            thread = threading.Thread(target=lambda path=effect: queued.append(supervisor.request(
                {"op": "act", "args": {"effect": str(path)}}, timeout=4)))
            thread.start()
            self.addCleanup(thread.join, 3)
        old_cancel = threading.Event()
        old_cancel.set()
        stopped = self.directory / "stop-invoked"
        result = supervisor.request_stop({"op": "act", "args": {"effect": str(stopped)}}, timeout=3)
        running.join(3)
        self.assertEqual(result["status"], "stopping")
        self.assertTrue(stopped.exists())
        self.assertFalse(any(path.exists() for path in effects))
        self.assertTrue(all(item["status"] in {"not_issued", "rejected"} for item in queued))

    def test_cancel_after_entered_preserves_unknown_not_false_completed(self):
        supervisor = self.supervisor()
        effect = self.directory / "admitted"
        cancel = threading.Event()
        results = []
        thread = threading.Thread(target=lambda: results.append(supervisor.request(
            {"op": "act", "args": {"effect": str(effect), "hang_after": True}},
            timeout=4, cancel_event=cancel)))
        thread.start()
        wait_for_file(effect)
        cancel.set()
        thread.join(3)
        self.assertEqual(results[0]["status"], "unknown")
        self.assertTrue(supervisor.health()["unknown"])

    def test_another_instance_cannot_overwrite_active_owners_crash_barrier(self):
        owner, other = self.supervisor(), self.supervisor()
        marker = self.directory / "entered"
        thread = threading.Thread(target=lambda: owner.request(
            {"op": "act", "args": {"effect": str(marker), "hang_after": True}}, timeout=5))
        thread.start()
        self.addCleanup(thread.join, 3)
        wait_for_file(marker)
        result = other.request_stop()
        self.assertEqual(result["status"], "unknown")
        state = json.loads((self.directory / "desktop.lock.state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["unknown"])

    def test_child_owned_lock_survives_parent_death_until_helper_really_exits(self):
        marker, release = self.directory / "orphan-entered", self.directory / "orphan-release"
        process = multiprocessing.get_context("spawn").Process(
            target=orphan_parent, args=(str(self.directory / "desktop.lock"), str(marker), str(release)))
        process.start()
        try:
            wait_for_file(marker)
            process.terminate()
            process.join(2)
            self.assertFalse(process.is_alive())
            replacement = self.supervisor()
            self.assertEqual(replacement.request({"op": "snapshot"}, timeout=1)["status"], "rejected")
            self.assertTrue(replacement.health()["unknown"])
            release.touch()
            deadline = time.monotonic() + 3
            result = {}
            while time.monotonic() < deadline:
                result = replacement.request({"op": "snapshot"}, timeout=1)
                if result["status"] == "completed":
                    break
                time.sleep(.02)
            self.assertEqual(result["status"], "completed")
            self.assertTrue(replacement.health()["unknown"])
        finally:
            release.touch()
            if process.is_alive():
                process.terminate()
                process.join(2)
            process.close()

    def test_real_maa_adapter_stop_observes_fresh_control_in_new_helper(self):
        supervisor = GuiSupervisor({"hwnd": 101, "pid": 202, "creation_time": 777},
                                   lock_path=self.directory / "desktop.lock", test_handler=OfflineMaaHandler())
        self.supervisors.append(supervisor)
        result = supervisor.request_stop({"op": "act", "args": {"action": "invoke"}}, timeout=3)
        self.assertEqual(result["status"], "stopping")
        self.assertTrue(result["submission_entered"])
        self.assertEqual(result["result"]["element_id"], "uia:42:1")

    def test_unconfirmed_helper_exit_freezes_rpc_and_never_hands_over(self):
        supervisor = self.supervisor()
        effect = self.directory / "live-child"
        results = []
        thread = threading.Thread(target=lambda: results.append(supervisor.request(
            {"op": "act", "args": {"effect": str(effect), "hang_after": True}}, timeout=3)))
        thread.start()
        wait_for_file(effect)
        process = supervisor._child
        with patch.object(process, "terminate", side_effect=OSError("termination denied")):
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0]["status"], "unknown")
            self.assertFalse(results[0]["helper_exit_confirmed"])
            result = supervisor.request({"op": "snapshot"}, timeout=.3)
            self.assertEqual(result["error"], "helper_exit_unconfirmed")
            self.assertEqual(supervisor.health()["child_pid"], process.pid)
            with self.assertRaisesRegex(RuntimeError, 'helper_exit_unconfirmed'):
                supervisor.close()


if __name__ == "__main__":
    unittest.main()

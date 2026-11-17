"""Real SQLite protocol tests; no GUI, model or network access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from shu.core import ActionRejected, TaskEngine
from shu.workflow_store import WorkflowStore


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.directory = Path(self.folder.name)
        self.engines = [TaskEngine(self.directory)]
        self.store_type = WorkflowStore
        self.store = self.store_type(self.engines[0])
        self.task = self.store.submit("导入 TL-2，禁止开始", {"allow_start": False})
        self.task_id = self.task["task_id"]

    def tearDown(self):
        for engine in self.engines:
            engine.close()
        self.folder.cleanup()

    def reopen(self):
        engine = TaskEngine(self.directory)
        self.engines.append(engine)
        return self.store_type(engine)

    def dispatch(self, key="import:TL-2", role="maa"):
        return self.store.ensure_dispatch(self.task_id, role, key, {"stage": "TL-2"})

    def operation(self, key="import:TL-2", **kwargs):
        job = self.dispatch()
        return self.store.prepare_operation(
            self.task_id, job["dispatch_id"], key, {"kind": "import"}, **kwargs,
        )

    def research(self):
        path = self.directory / 'copilot.json'
        path.write_text('{"stage_name":"TL-2","actions":[{}]}', encoding='utf-8')
        ref = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        payload = {'task': 'Find TL-2', 'contract': self.store.current(self.task_id)['contract']}
        job = self.store.ensure_dispatch(self.task_id, 'research', 'source-research', payload)
        self.store.finish_dispatch(job['dispatch_id'], {'status': 'completed', 'candidate_refs': [ref]})
        return job, path, ref

    def test_explicit_research_adoption_is_current_durable_and_idempotent(self):
        source, path, ref = self.research()
        self.store.control(self.task_id, 'pause')
        current = self.store.control(self.task_id, 'resume')
        adopted = self.store.adopt_research(self.task_id, source['dispatch_id'], 'adopt:one', input_task='Find TL-2')
        self.assertEqual(adopted['epoch'], current['epoch'])
        result = self.store.dispatch_result(adopted['dispatch_id'])
        self.assertEqual(result['candidate_refs'], [ref])
        self.assertEqual(result['adoption']['source_dispatch_id'], source['dispatch_id'])
        self.assertEqual(self.reopen().adopt_research(self.task_id, source['dispatch_id'], 'adopt:one',
                                                    input_task='Find TL-2')['dispatch_id'], adopted['dispatch_id'])
        self.assertTrue(self.store.research_adoption_matches(adopted['dispatch_id'], source['dispatch_id'], ref))
        self.assertEqual(len([e for e in self.store.pending_events(self.task_id) if e['type'] == 'research.adopted']), 1)

    def test_adoption_rejects_changed_file_contract_or_input(self):
        source, path, ref = self.research()
        self.store.control(self.task_id, 'pause')
        self.store.control(self.task_id, 'resume')
        with self.assertRaises(ActionRejected):
            self.store.adopt_research(self.task_id, source['dispatch_id'], 'wrong-input', input_task='Find TL-3')
        original = path.read_bytes()
        path.write_text('changed', encoding='utf-8')
        with self.assertRaises((ValueError, ActionRejected)):
            self.store.adopt_research(self.task_id, source['dispatch_id'], 'changed-file', input_task='Find TL-2')
        path.write_bytes(original)
        self.store.submit('TL-3', {'allow_start': False, 'profile_id': 'other', 'box_revision': 2}, self.task_id)
        with self.assertRaises(ActionRejected):
            self.store.adopt_research(self.task_id, source['dispatch_id'], 'changed-contract', input_task='Find TL-2')

    def test_adoption_rejects_expired_or_unregistered_artifact(self):
        source, path, ref = self.research()
        self.store.control(self.task_id, 'pause')
        self.store.control(self.task_id, 'resume')
        self.engines[0]._db.execute("UPDATE workflow_dispatches SET created_at='2020-01-01T00:00:00+00:00' WHERE dispatch_id=?",
                                  (source['dispatch_id'],))
        with self.assertRaises(ActionRejected):
            self.store.adopt_research(self.task_id, source['dispatch_id'], 'expired', input_task='Find TL-2')
        self.engines[0]._db.execute('UPDATE workflow_dispatches SET created_at=? WHERE dispatch_id=?',
                                  (datetime.now(timezone.utc).isoformat(), source['dispatch_id']))
        self.engines[0]._db.execute('DELETE FROM workflow_research_artifacts WHERE dispatch_id=?', (source['dispatch_id'],))
        with self.assertRaises(ActionRejected):
            self.store.adopt_research(self.task_id, source['dispatch_id'], 'unregistered', input_task='Find TL-2')

    def test_adoption_checks_download_behind_unchanged_provenance_file(self):
        downloaded = self.directory / 'copilot.json'
        downloaded.write_text('{"stage_name":"TL-2","actions":[{}]}', encoding='utf-8')
        record = self.directory / 'provenance.json'
        record.write_text(json.dumps({'path': str(downloaded),
            'sha256': hashlib.sha256(downloaded.read_bytes()).hexdigest()}), encoding='utf-8')
        ref = {'path': str(record), 'sha256': hashlib.sha256(record.read_bytes()).hexdigest()}
        job = self.store.ensure_dispatch(self.task_id, 'research', 'downloaded-research',
            {'task': 'Find TL-2', 'contract': self.store.current(self.task_id)['contract']})
        self.store.finish_dispatch(job['dispatch_id'], {'status': 'completed', 'candidate_refs': [ref]})
        self.store.control(self.task_id, 'pause')
        self.store.control(self.task_id, 'resume')
        downloaded.write_text('tampered downloaded file', encoding='utf-8')
        with self.assertRaises((ValueError, ActionRejected)):
            self.store.adopt_research(self.task_id, job['dispatch_id'], 'changed-download', input_task='Find TL-2')

    def test_abandoned_maa_audit_keeps_submitted_operation_barrier(self):
        job = self.dispatch('abandoned-import', 'maa')
        self.store.register_runner('dead-runner')
        self.store.claim_dispatch(job['dispatch_id'], 'dead-runner')
        operation = self.store.prepare_operation(self.task_id, job['dispatch_id'], 'unknown-import', {'kind': 'import'})
        self.store.issue_operation(operation['operation_id'])
        self.store.enter_submission(operation['operation_id'])
        self.store.release_runner('dead-runner')
        self.store.audit_abandoned_dispatches(self.task_id)
        self.assertEqual(self.store.get_dispatch(job['dispatch_id'])['status'], 'unknown')
        self.assertEqual(self.store.slot_state()['barrier_operation_id'], operation['operation_id'])
        self.assertEqual(self.store.get_operation(operation['operation_id'])['status'], 'unknown')

    def test_unregistered_or_unreadable_owner_identity_is_not_death_proof(self):
        from unittest.mock import patch
        unregistered = self.dispatch('unregistered', 'research')
        self.store.claim_dispatch(unregistered['dispatch_id'], 'old-unregistered-worker')
        registered = self.dispatch('registered', 'research')
        self.store.register_runner('registered')
        self.store.claim_dispatch(registered['dispatch_id'], 'registered')
        with patch('shu.workflow_store._process_identity', side_effect=PermissionError('not death proof')):
            self.assertEqual(self.store.audit_abandoned_dispatches(self.task_id), [])
        for job in (unregistered, registered):
            self.assertEqual(self.store.get_dispatch(job['dispatch_id'])['status'], 'running')

    def test_slow_adoption_file_validation_does_not_block_trusted_stop(self):
        source, path, ref = self.research()
        self.store.control(self.task_id, 'pause')
        self.store.control(self.task_id, 'resume')
        adopted = self.store.adopt_research(self.task_id, source['dispatch_id'], 'current', input_task='Find TL-2')
        started, release = threading.Event(), threading.Event()
        original = self.store._research_artifact_refs
        def slow(result):
            started.set()
            release.wait(5)
            return original(result)
        self.store._research_artifact_refs = slow
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                validation = pool.submit(self.store.research_adoption_matches,
                                         adopted['dispatch_id'], source['dispatch_id'], ref)
                self.assertTrue(started.wait(2))
                stop = pool.submit(self.store.control, self.task_id, 'stop')
                try:
                    self.assertFalse(stop.result(timeout=1)['writes_enabled'])
                finally:
                    release.set()
                self.assertFalse(validation.result(timeout=2))
        finally:
            release.set()
            self.store._research_artifact_refs = original

    def test_owner_exit_audit_uses_registered_process_identity_and_never_replays(self):
        job = self.dispatch('owner-exit', 'research')
        script = ('from pathlib import Path; from shu.core import TaskEngine; '
                  'from shu.workflow_store import WorkflowStore; '
                  'e=TaskEngine(Path(__import__("sys").argv[1])); s=WorkflowStore(e); '
                  's.register_runner("child"); s.claim_dispatch(__import__("sys").argv[2], "child"); e.close()')
        # Keep the process handle open: Windows can still return creation times
        # for its terminated object until every outstanding handle is closed.
        with subprocess.Popen([sys.executable, '-c', script, str(self.directory), job['dispatch_id']]) as child:
            self.assertEqual(child.wait(timeout=10), 0)
            recovered = self.reopen().audit_abandoned_dispatches(self.task_id)
        self.assertEqual(recovered[0]['status'], 'interrupted')
        self.assertEqual(self.store.dispatch_result(job['dispatch_id'])['code'], 'owner_exited')
        self.assertIsNone(self.store.claim_dispatch(job['dispatch_id'], 'replacement'))
        self.assertEqual(self.store.audit_abandoned_dispatches(self.task_id), [])

    def test_same_contract_research_rejects_new_registered_box_revision(self):
        from shu.maa_facts import RosterStore
        roster = RosterStore(self.engines[0])
        roster.publish('fixture-profile', [], True, {'kind': 'fixture', 'observed_at': 100, 'evidence_ref': 'fixture:1'})
        self.store.submit('TL-2', {'allow_start': False, 'profile_id': 'fixture-profile'}, self.task_id)
        path = self.directory / 'box-sensitive-research.json'
        path.write_text('{"stage_name":"TL-2","actions":[{}]}', encoding='utf-8')
        ref = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        payload = {'task': 'Find TL-2', 'contract': self.store.current(self.task_id)['contract'],
                   **self.store.research_account(self.task_id)}
        source = self.store.ensure_dispatch(self.task_id, 'research', 'box-input', payload)
        self.store.finish_dispatch(source['dispatch_id'], {'status': 'completed', 'candidate_refs': [ref]})
        self.store.control(self.task_id, 'pause')
        self.store.control(self.task_id, 'resume')
        self.store.adopt_research(self.task_id, source['dispatch_id'], 'same-box', input_task='Find TL-2')
        adopted = self.store.adopt_research(self.task_id, source['dispatch_id'], 'same-box', input_task='Find TL-2')
        roster.publish('fixture-profile', [], True, {'kind': 'fixture', 'observed_at': 101, 'evidence_ref': 'fixture:2'})
        with self.assertRaises(ActionRejected):
            self.store.adopt_research(self.task_id, source['dispatch_id'], 'changed-box', input_task='Find TL-2')
        self.assertFalse(self.store.research_adoption_matches(adopted['dispatch_id'], source['dispatch_id'], ref))

    def test_owner_audit_preserves_live_owner_and_unknown_maa_effect(self):
        self.store.register_runner('live')
        job = self.dispatch('live', 'research')
        self.store.claim_dispatch(job['dispatch_id'], 'live')
        self.assertEqual(self.store.audit_abandoned_dispatches(self.task_id), [])
        maa = self.dispatch('dead-maa', 'maa')
        self.store.register_runner('reused-pid')
        self.store.claim_dispatch(maa['dispatch_id'], 'reused-pid')
        self.engines[0]._db.execute("UPDATE workflow_runners SET process_identity='different-creation' WHERE owner='reused-pid'")
        recovered = self.store.audit_abandoned_dispatches(self.task_id)
        self.assertEqual(recovered[0]['status'], 'unknown')
        self.assertEqual(self.store.dispatch_result(maa['dispatch_id'])['requires_diagnosis'], True)

    def test_committed_result_reused_after_lost_graph_and_database_reopen(self):
        job = self.dispatch("find:TL-2:box1", "research")
        self.store.finish_dispatch(job["dispatch_id"], {"artifact_ref": "synthetic-fixture"})
        reopened = self.reopen()
        same = reopened.ensure_dispatch(
            self.task_id, "research", "find:TL-2:box1", {"stage": "TL-2"},
        )
        self.assertEqual(same["dispatch_id"], job["dispatch_id"])
        self.assertEqual(reopened.dispatch_result(same["dispatch_id"]),
                         {"artifact_ref": "synthetic-fixture"})
        self.assertIsNone(reopened.claim_dispatch(same["dispatch_id"], "new-graph"))

    def test_contract_pointer_task_epoch_and_outbox_commit_together(self):
        changed = self.store.submit("改导入 TL-3", {"allow_start": False}, self.task_id)
        self.assertEqual((changed["revision"], changed["epoch"]), (2, 2))
        self.assertEqual(self.engines[0].get_task(self.task_id)["version"], 2)
        events = self.reopen().pending_events(self.task_id)
        matching = [e for e in events if e["type"] == "command.submitted" and e["revision"] == 2]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["payload"]["goal"], "改导入 TL-3")

    def test_contract_publication_failure_rolls_back_task_and_current_pointer(self):
        self.engines[0]._db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON workflow_outbox "
                                   "BEGIN SELECT RAISE(ABORT, 'disk full fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.submit("改导入 TL-3", {"allow_start": False}, self.task_id)
        self.assertEqual(self.store.current(self.task_id)["revision"], 1)
        self.assertEqual(self.engines[0].get_task(self.task_id)["version"], 1)

    def test_dispatch_key_collision_rejects_different_payload_or_role(self):
        job = self.dispatch()
        for role, payload in (("maa", {"stage": "TL-3"}), ("research", {"stage": "TL-2"})):
            with self.assertRaises(ValueError):
                self.store.ensure_dispatch(self.task_id, role, "import:TL-2", payload)
        self.assertEqual(self.dispatch()["dispatch_id"], job["dispatch_id"])

    def test_one_immutable_result_and_one_result_event(self):
        job = self.dispatch()
        self.store.finish_dispatch(job["dispatch_id"], {"verified": True})
        self.store.finish_dispatch(job["dispatch_id"], {"verified": True})
        with self.assertRaises(ValueError):
            self.store.finish_dispatch(job["dispatch_id"], {"verified": False})
        events = [e for e in self.store.pending_events() if e["type"] == "dispatch.result"]
        self.assertEqual(len(events), 1)

    def test_inbox_duplicate_does_not_hide_result_until_checkpoint_ack(self):
        job = self.dispatch()
        self.store.finish_dispatch(job["dispatch_id"], {"evidence_ref": "result-1"})
        event = next(e for e in self.store.pending_events() if e["type"] == "dispatch.result")
        self.assertTrue(self.store.accept_event("graph", event["event_id"]))
        self.assertFalse(self.reopen().accept_event("graph", event["event_id"]))
        self.assertEqual(self.store.dispatch_result(job["dispatch_id"]), {"evidence_ref": "result-1"})
        self.assertIn(event["event_id"], [e["event_id"] for e in self.store.pending_events()])
        self.store.ack_event(event["event_id"])
        self.assertNotIn(event["event_id"], [e["event_id"] for e in self.store.pending_events()])

    def test_stale_epoch_result_kept_but_not_applied_and_revision_unchanged_on_pause(self):
        job = self.dispatch()
        paused = self.store.control(self.task_id, "pause")
        self.store.finish_dispatch(job["dispatch_id"], {"evidence_ref": "late"})
        self.assertEqual((paused["revision"], paused["epoch"], paused["writes_enabled"]), (1, 2, False))
        self.assertIsNone(self.store.dispatch_result(job["dispatch_id"]))
        self.assertEqual(self.store.dispatch_result(job["dispatch_id"], current_only=False), {"evidence_ref": "late"})
        self.assertIsNone(self.store.claim_dispatch(job["dispatch_id"], "late-worker"))

    def test_concurrent_connections_have_one_dispatch_claim(self):
        job = self.dispatch()
        other = self.reopen()
        barrier = threading.Barrier(2)
        def claim(store, owner):
            barrier.wait()
            return store.claim_dispatch(job["dispatch_id"], owner)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(claim, self.store, "one")
            two = pool.submit(claim, other, "two")
            results = [one.result(), two.result()]
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_external_taskengine_version_change_fences_store_dispatch(self):
        job = self.dispatch()
        self.engines[0].update_goal(self.task_id, "trusted legacy goal change")
        self.assertIsNone(self.store.claim_dispatch(job["dispatch_id"], "old"))
        with self.assertRaises(ActionRejected):
            self.store.prepare_operation(self.task_id, job["dispatch_id"], "late", {"kind": "import"})

    def test_operation_effect_is_stable_and_cannot_be_issued_twice(self):
        operation = self.operation()
        self.assertEqual(self.operation()["operation_id"], operation["operation_id"])
        issued = self.store.issue_operation(operation["operation_id"])
        self.assertEqual(issued["status"], "issued")
        with self.assertRaises(ActionRejected):
            self.reopen().issue_operation(operation["operation_id"])
        with self.assertRaises(ValueError):
            self.store.prepare_operation(self.task_id, self.dispatch()["dispatch_id"],
                                         "import:TL-2", {"kind": "different"})

    def test_stop_between_issue_and_submission_prevents_actual_input(self):
        operation = self.operation()
        self.store.issue_operation(operation["operation_id"])
        stopped = self.reopen().control(self.task_id, "stop")
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation["operation_id"])
        self.assertFalse(stopped["writes_enabled"])
        self.assertIsNone(self.store.slot_state()["barrier_operation_id"])
        self.assertEqual(self.operation()["status"], "not_issued")

    def test_submission_before_stop_remains_unknown_across_new_task_and_restart(self):
        operation = self.operation()
        self.store.issue_operation(operation["operation_id"])
        self.store.enter_submission(operation["operation_id"])
        self.store.control(self.task_id, "stop")
        other = self.reopen()
        fresh = other.submit("新任务", {"allow_start": False})
        job = other.ensure_dispatch(fresh["task_id"], "maa", "new-import", {})
        with self.assertRaises(ActionRejected):
            other.prepare_operation(fresh["task_id"], job["dispatch_id"], "new-effect", {"kind": "import"})
        self.assertEqual(other.slot_state()["barrier_operation_id"], operation["operation_id"])
        with self.assertRaises(ActionRejected):
            other.finish_operation(operation["operation_id"], "not_issued", {})

    def test_only_maa_role_can_prepare_gui_and_import_contract_cannot_start(self):
        researcher = self.dispatch("research", "research")
        with self.assertRaises(ActionRejected):
            self.store.prepare_operation(self.task_id, researcher["dispatch_id"], "forged", {"kind": "import"})
        with self.assertRaises(ActionRejected):
            self.operation(starts=True)

    def test_start_reservation_survives_restart_even_when_stopped_before_submission(self):
        self.store.submit("fixture only, no real GUI", {"allow_start": True, "attempt_limit": 1}, self.task_id)
        operation = self.operation(starts=True)
        self.store.issue_operation(operation["operation_id"])
        self.store.control(self.task_id, "pause")
        reopened = self.reopen()
        reopened.control(self.task_id, "resume")
        job = reopened.ensure_dispatch(self.task_id, "maa", "retry", {})
        retry = reopened.prepare_operation(self.task_id, job["dispatch_id"], "retry-start", {"kind": "start"}, starts=True)
        with self.assertRaises(ActionRejected):
            reopened.issue_operation(retry["operation_id"])
        self.assertEqual(reopened.slot_state()["attempts_reserved"], 1)

    def test_verified_ordinary_operation_releases_slot_and_submission_is_once(self):
        operation = self.operation()
        self.store.issue_operation(operation["operation_id"])
        self.store.enter_submission(operation["operation_id"])
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation["operation_id"])
        self.store.finish_operation(operation["operation_id"], "verified", {"evidence_ref": "readback"})
        self.assertIsNone(self.store.slot_state()["barrier_operation_id"])

    def test_trusted_stop_only_can_clear_unknown_without_restoring_ordinary_writes(self):
        operation = self.operation()
        self.store.issue_operation(operation["operation_id"])
        self.store.enter_submission(operation["operation_id"])
        stopped = self.store.control(self.task_id, "stop")
        stop = self.store.prepare_control_operation(self.task_id, stopped["control_seq"],
                                                    "stop:1", {"kind": "stop"})
        self.store.issue_operation(stop["operation_id"])
        self.store.enter_submission(stop["operation_id"])
        self.store.finish_operation(stop["operation_id"], "verified", {"stopped": True})
        self.assertIsNone(self.store.slot_state()["barrier_operation_id"])
        self.assertFalse(self.store.current(self.task_id)["writes_enabled"])

    def test_json_sensitive_fields_objects_and_nan_rejected_before_persistence(self):
        for payload in ({"api_key": "secret"}, {"nested": {"reasoning_content": "hidden"}},
                        {"client": object()}, {"score": float("nan")}, {"image": "data:image/png;base64,AAAA"}):
            with self.subTest(payload_type=next(iter(payload))):
                with self.assertRaises((ValueError, TypeError)):
                    self.store.ensure_dispatch(self.task_id, "research", "bad", payload)
        self.assertFalse(any(e["payload"].get("key") == "bad" for e in self.store.pending_events()))

    def test_fact_outbox_event_collision_and_inbox_payload_integrity(self):
        engine = self.engines[0]
        with engine._transaction():
            event = self.store._publish_event(None, "box.updated", {"box_revision": 1}, event_key="box:one")
        with engine._transaction():
            duplicate = self.store._publish_event(None, "box.updated", {"box_revision": 1}, event_key="box:one")
        self.assertEqual(duplicate["event_id"], event["event_id"])
        with self.assertRaises(ValueError), engine._transaction():
            self.store._publish_event(None, "box.updated", {"box_revision": 2}, event_key="box:one")
        self.store.accept_event("graph", event["event_id"])
        engine._db.execute("UPDATE workflow_outbox SET payload=? WHERE event_id=?",
                           (json.dumps({"box_revision": 99}), event["event_id"]))
        with self.assertRaises(ValueError):
            self.store.accept_event("graph", event["event_id"])

    def test_result_event_failure_rolls_back_result_and_dispatch_status(self):
        job = self.dispatch()
        self.engines[0]._db.execute("CREATE TRIGGER fail_result BEFORE INSERT ON workflow_outbox "
                                   "WHEN NEW.type='dispatch.result' BEGIN SELECT RAISE(ABORT, 'disk full fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish_dispatch(job["dispatch_id"], {"artifact_ref": "durable-first"})
        self.assertIsNone(self.store.dispatch_result(job["dispatch_id"]))
        self.assertEqual(self.store.get_dispatch(job["dispatch_id"])["status"], "pending")

    def test_failed_issue_rolls_back_attempt_reservation_and_cannot_enter_gui(self):
        self.store.submit("offline fixture", {"allow_start": True, "attempt_limit": 1}, self.task_id)
        operation = self.operation(starts=True)
        self.engines[0]._db.execute("CREATE TRIGGER fail_issue BEFORE UPDATE ON workflow_operations "
                                   "WHEN NEW.status='issued' BEGIN SELECT RAISE(ABORT, 'disk full fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.issue_operation(operation["operation_id"])
        self.assertEqual(self.store.slot_state()["attempts_reserved"], 0)
        with self.assertRaises(ActionRejected):
            self.store.enter_submission(operation["operation_id"])

    def test_start_action_cannot_hide_start_flag_or_forge_stop_control(self):
        job = self.dispatch()
        with self.assertRaises(ActionRejected):
            self.store.prepare_operation(self.task_id, job["dispatch_id"], "hidden-start", {"kind": "start"})
        with self.assertRaises(ActionRejected):
            self.store.prepare_control_operation(self.task_id, 0, "forged-stop", {"kind": "stop"})

    def test_ordinary_idle_stop_does_not_erase_unknown_start_or_its_reservation(self):
        self.store.submit("offline fixture", {"allow_start": True, "attempt_limit": 1}, self.task_id)
        operation = self.operation(starts=True)
        self.store.issue_operation(operation["operation_id"])
        self.store.enter_submission(operation["operation_id"])
        stopped = self.store.control(self.task_id, "stop")
        stop = self.store.prepare_control_operation(self.task_id, stopped["control_seq"], "stop:1", {"kind": "stop"})
        self.store.issue_operation(stop["operation_id"])
        self.store.enter_submission(stop["operation_id"])
        self.store.finish_operation(stop["operation_id"], "verified", {"stopped": True})
        state = self.reopen().slot_state()
        self.assertEqual(state["barrier_operation_id"], operation["operation_id"])
        self.assertEqual(state["attempts_reserved"], 1)

    def test_stale_stop_receipt_cannot_clear_another_stop_or_new_operation_barrier(self):
        stopped = self.store.control(self.task_id, "stop")
        old = self.store.prepare_control_operation(self.task_id, stopped["control_seq"], "old-stop", {"kind": "stop"})
        self.store.issue_operation(old["operation_id"])
        self.store.enter_submission(old["operation_id"])
        stopped = self.store.control(self.task_id, "stop")
        fresh = self.store.prepare_control_operation(self.task_id, stopped["control_seq"], "fresh-stop", {"kind": "stop"})
        self.store.issue_operation(fresh["operation_id"])
        self.store.enter_submission(fresh["operation_id"])
        self.store.finish_operation(fresh["operation_id"], "verified", {"stopped": True})
        self.store.control(self.task_id, "resume")
        job = self.store.ensure_dispatch(self.task_id, "maa", "fresh-import", {})
        operation = self.store.prepare_operation(self.task_id, job["dispatch_id"], "fresh-import", {"kind": "import"})
        self.store.issue_operation(operation["operation_id"])
        self.store.finish_operation(old["operation_id"], "verified", {"stopped": True})
        self.assertEqual(self.store.slot_state()["barrier_operation_id"], operation["operation_id"])

    def test_cancelled_unsubmitted_effect_cannot_be_relabelled_unknown(self):
        operation = self.operation()
        self.store.issue_operation(operation["operation_id"])
        self.store.control(self.task_id, "stop")
        with self.assertRaises(ActionRejected):
            self.store.finish_operation(operation["operation_id"], "unknown", {"message": "late worker"})

    def test_tampered_contract_fails_closed_instead_of_granting_start(self):
        self.engines[0]._db.execute("UPDATE workflow_contracts SET contract=? WHERE task_id=?", (
            json.dumps({"allow_start": True, "attempt_limit": 99}), self.task_id,
        ))
        with self.assertRaises(ValueError):
            self.store.current(self.task_id)

    def test_stale_stop_receipt_cannot_restore_its_own_slot_after_resume(self):
        stopped = self.store.control(self.task_id, "stop")
        stop = self.store.prepare_control_operation(self.task_id, stopped["control_seq"], "stop:old", {"kind": "stop"})
        self.store.issue_operation(stop["operation_id"])
        self.store.enter_submission(stop["operation_id"])
        self.store.control(self.task_id, "resume")
        self.store.finish_operation(stop["operation_id"], "verified", {"stopped": True})
        self.assertEqual(self.store.slot_state()["barrier_operation_id"], stop["operation_id"])

    def test_concurrent_connections_reserve_effect_once(self):
        operation = self.operation()
        other = self.reopen()
        barrier = threading.Barrier(2)
        def issue(store):
            barrier.wait()
            try:
                store.issue_operation(operation["operation_id"])
                return True
            except ActionRejected:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(issue, self.store)
            two = pool.submit(issue, other)
            results = [one.result(), two.result()]
        self.assertEqual(results.count(True), 1)

    def test_old_ordinary_receipt_saved_without_clearing_current_reconciliation_barrier(self):
        operation = self.operation()
        self.store.issue_operation(operation["operation_id"])
        self.store.enter_submission(operation["operation_id"])
        self.store.control(self.task_id, "pause")
        self.store.finish_operation(operation["operation_id"], "verified", {"evidence_ref": "late"})
        self.assertEqual(self.store.get_operation(operation["operation_id"])["result"], {"evidence_ref": "late"})
        self.assertEqual(self.store.slot_state()["barrier_operation_id"], operation["operation_id"])

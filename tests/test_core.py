"""Task lifecycle tests: old callbacks, real evidence, cancellation and durability."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import time
import unittest

from shu.core import ActionRejected, TaskEngine, TaskNotFound


class TaskEngineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.folder.name)
        self.engine = TaskEngine(self.data_dir)
        self.task = self.engine.submit_goal("刷六次，不使用药剂")
        self.task_id = self.task["id"]

    def tearDown(self):
        self.engine.close()
        self.folder.cleanup()

    def observe(self, identifier="frame-1", *, state="home", captured_at=None):
        task = self.engine.get_task(self.task_id)
        return self.engine.record_observation(
            self.task_id, identifier, captured_at or time.time(), "window-123", state,
            goal_version=task["version"],
        )

    def activate(self, *, state="home", identifier="frame-1"):
        self.engine.set_status(self.task_id, "observing", goal_version=self.task["version"])
        self.observe(identifier, state=state)
        return self.engine.get_task(self.task_id)

    def test_visual_goal_check_does_not_recognize_settlement_or_increment_count(self):
        self.engine.set_status(self.task_id,'running',goal_version=1)
        self.observe(state='unknown')
        with self.assertRaises(ActionRejected):
            self.engine.complete_visual_goal(self.task_id,goal_version=1,observation_id='old',evidence='主页可见')
        with self.assertRaises(ActionRejected):
            self.engine.confirmed_battle_result(self.task_id,'invented',goal_version=1)
        task=self.engine.complete_visual_goal(self.task_id,goal_version=1,observation_id='frame-1',evidence='主页可见')
        self.assertEqual((task['status'],task['completed_runs']),('completed',0))
        self.assertEqual(task['latest_observation_state'],'unknown')

    def test_visual_goal_check_rejects_old_goal_and_stale_evidence(self):
        self.engine.set_status(self.task_id,'running',goal_version=1)
        self.observe(state='unknown',captured_at=time.time()-181)
        with self.assertRaises(ActionRejected):
            self.engine.complete_visual_goal(self.task_id,goal_version=1,observation_id='frame-1',evidence='旧画面')
        self.engine.update_goal(self.task_id,'新目标')
        with self.assertRaises(ActionRejected):
            self.engine.complete_visual_goal(self.task_id,goal_version=1,observation_id='frame-1',evidence='旧画面')

    def test_new_goal_is_draft_and_chat_does_not_create_progress(self):
        self.assertEqual(self.task["status"], "draft")
        self.assertEqual(self.task["completed_runs"], 0)
        self.assertEqual(self.task["text"], self.task["goal"])
        self.engine.record_event(self.task_id, "assistant_message", "已经完成六次")
        self.assertEqual(self.engine.get_task(self.task_id)["completed_runs"], 0)
        with self.assertRaises(ActionRejected):
            self.engine.set_status(self.task_id, "completed")
        with self.assertRaises(ActionRejected):
            self.engine.validate_action(self.task_id, 1, "missing")

    def test_unknown_observation_cannot_authorize_action_or_completion(self):
        self.activate(state="unknown")
        with self.assertRaises(ActionRejected):
            self.engine.validate_action(self.task_id, 1, "frame-1")
        with self.assertRaises(ActionRejected):
            self.engine.set_status(self.task_id, "completed")

    def test_goal_change_invalidates_observation_and_old_callbacks(self):
        self.activate()
        self.assertTrue(self.engine.validate_action(self.task_id, 1, "frame-1"))
        changed = self.engine.update_goal(self.task_id, "改成两次")
        self.assertEqual(changed["version"], 2)
        self.assertEqual(changed["status"], "draft")
        self.assertIsNone(changed["latest_observation_id"])
        for callback in (
            lambda: self.engine.validate_action(self.task_id, 1, "frame-1"),
            lambda: self.engine.set_status(self.task_id, "running", goal_version=1),
            lambda: self.engine.record_observation(self.task_id, "late", time.time(), "window-123", "home", goal_version=1),
            lambda: self.engine.confirmed_battle_result(self.task_id, "old-result", goal_version=1),
        ):
            with self.assertRaises(ActionRejected):
                callback()
        self.assertEqual(self.engine.get_task(self.task_id)["status"], "draft")

    def test_stale_observation_is_rejected_without_sleep(self):
        self.engine.set_status(self.task_id, "running")
        self.observe(captured_at=time.time() - 20)
        with self.assertRaisesRegex(ActionRejected, "过期"):
            self.engine.validate_action(self.task_id, 1, "frame-1")
        with self.assertRaises(ActionRejected):
            self.engine.set_status(self.task_id, "completed")

    def test_action_requires_an_integer_goal_version(self):
        self.activate()
        for invalid in (None, True, 1.0, "1", 0):
            with self.assertRaises(ActionRejected):
                self.engine.validate_action(self.task_id, invalid, "frame-1")

    def test_newer_observation_supersedes_old_and_out_of_order_frame_is_rejected(self):
        self.activate()
        self.observe("frame-2")
        with self.assertRaises(ActionRejected):
            self.engine.validate_action(self.task_id, 1, "frame-1")
        self.assertTrue(self.engine.validate_action(self.task_id, 1, "frame-2"))
        with self.assertRaises(ActionRejected):
            self.observe("delayed", captured_at=time.time() - 1)
        self.assertEqual(self.engine.get_task(self.task_id)["latest_observation_id"], "frame-2")

    def test_pause_resume_requires_a_new_observation_and_version(self):
        self.activate()
        paused = self.engine.pause_task(self.task_id)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["version"], 2)
        with self.assertRaises(ActionRejected):
            self.engine.validate_action(self.task_id, 1, "frame-1")
        resumed = self.engine.resume_task(self.task_id)
        self.assertEqual(resumed["status"], "observing")
        self.assertEqual(resumed["version"], 3)
        with self.assertRaises(ActionRejected):
            self.engine.validate_action(self.task_id, 3, "frame-1")
        self.observe("after-resume")
        self.assertTrue(self.engine.validate_action(self.task_id, 3, "after-resume"))

    def test_stop_blocks_callbacks_and_new_progress(self):
        self.activate(state="battle_result")
        stopped = self.engine.stop_task(self.task_id)
        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual(stopped["version"], 2)
        with self.assertRaises(ActionRejected):
            self.engine.set_status(self.task_id, "running", goal_version=1)
        with self.assertRaises(ActionRejected):
            self.engine.confirmed_battle_result(self.task_id, "late-result")
        self.assertEqual(self.engine.stop_task(self.task_id)["version"], 2)
        self.assertEqual(self.engine.get_task(self.task_id)["completed_runs"], 0)

    def test_stop_without_id_selects_newest_unfinished_task(self):
        self.activate()
        newer = self.engine.submit_goal("观察画面")
        self.assertEqual(self.engine.stop_task()["id"], newer["id"])
        self.assertEqual(self.engine.stop_task()["id"], self.task_id)
        self.assertIsNone(self.engine.stop_task())

    def test_result_requires_recognized_settlement(self):
        self.activate(state="home")
        with self.assertRaises(ActionRejected):
            self.engine.confirmed_battle_result(self.task_id, "result-1")
        self.assertEqual(self.engine.get_task(self.task_id)["completed_runs"], 0)

    def test_settlement_is_counted_once_even_across_threads(self):
        self.activate(state="battle_result")
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(
                lambda _: self.engine.confirmed_battle_result(self.task_id, "same-result", goal_version=1),
                range(24),
            ))
        self.assertEqual(sum(item["result_recorded"] for item in outcomes), 1)
        self.assertEqual(self.engine.get_task(self.task_id)["completed_runs"], 1)
        self.assertEqual(sum(e["type"] == "battle_result_confirmed" for e in self.engine.events(self.task_id)), 1)

    def test_same_settlement_observation_cannot_be_relabelled_as_a_new_result(self):
        self.activate(state="battle_result")
        self.engine.confirmed_battle_result(self.task_id, "result-1")
        with self.assertRaises(ActionRejected):
            self.engine.confirmed_battle_result(self.task_id, "another-id")
        self.assertEqual(self.engine.get_task(self.task_id)["completed_runs"], 1)

    def test_waiting_for_input_clears_action_evidence(self):
        self.activate()
        waiting = self.engine.set_status(self.task_id, "needs_input", goal_version=1)
        self.assertEqual(waiting["version"], 2)
        self.assertIsNone(waiting["latest_observation_id"])
        self.engine.set_status(self.task_id, "observing", goal_version=2)
        with self.assertRaises(ActionRejected):
            self.engine.validate_action(self.task_id, 2, "frame-1")

    def test_state_events_and_result_dedup_survive_reopen(self):
        self.activate(state="battle_success")
        self.engine.confirmed_battle_result(self.task_id, "result-1")
        expected_events = self.engine.events(self.task_id)
        self.engine.close()
        self.engine = TaskEngine(self.data_dir)
        self.assertEqual(self.engine.get_task(self.task_id)["completed_runs"], 1)
        self.assertEqual(self.engine.events(self.task_id), expected_events)
        result = self.engine.confirmed_battle_result(self.task_id, "result-1")
        self.assertFalse(result["result_recorded"])
        self.assertEqual(result["completed_runs"], 1)

    def test_separate_connections_do_not_double_count_same_result(self):
        self.activate(state="settlement")
        other = TaskEngine(self.data_dir)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(engine.confirmed_battle_result, self.task_id, "shared-result")
                           for engine in (self.engine, other)]
                outcomes = [f.result() for f in futures]
            self.assertEqual(sum(x["result_recorded"] for x in outcomes), 1)
            self.assertEqual(self.engine.get_task(self.task_id)["completed_runs"], 1)
        finally:
            other.close()

    def test_recovery_invalidates_active_tasks_before_application_resumes(self):
        self.activate()
        recovered = self.engine.recover_interrupted_tasks()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "needs_input")
        self.assertEqual(recovered[0]["version"], 2)
        self.assertIsNone(recovered[0]["latest_observation_id"])
        with self.assertRaises(ActionRejected):
            self.engine.validate_action(self.task_id, 1, "frame-1")

    def test_bad_inputs_and_event_serialization_fail_atomically(self):
        with self.assertRaises(ValueError):
            self.engine.submit_goal("  ")
        with self.assertRaises(TaskNotFound):
            self.engine.get_task("missing")
        before = self.engine.events(self.task_id)
        with self.assertRaises(ValueError):
            self.engine.record_event(self.task_id, "bad", "invalid JSON", {"value": float("nan")})
        self.assertEqual(self.engine.events(self.task_id), before)
        with self.assertRaises(ValueError):
            self.engine.record_observation(self.task_id, "bad-time", False, "window-123")
        with self.assertRaises(ActionRejected):
            self.engine.record_observation(self.task_id, "future", time.time()+60, "window-123")

    def test_iso_timestamp_and_recognized_completion(self):
        self.engine.set_status(self.task_id, "observing")
        self.engine.record_observation(
            self.task_id, "iso-frame", datetime.now(timezone.utc).isoformat(), 123, "home",
        )
        self.assertTrue(self.engine.validate_action(self.task_id, 1, "iso-frame"))
        completed = self.engine.set_status(self.task_id, "completed", "已确认观察目标完成", goal_version=1)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["completed_runs"], 0)
        with self.assertRaises(ActionRejected):
            self.engine.update_goal(self.task_id, "重新执行")


if __name__ == "__main__":
    unittest.main()

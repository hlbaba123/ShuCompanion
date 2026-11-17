"""Task identity checks must serialize reads on their shared SQLite connection."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from shu.core import ActionRejected, TaskEngine, TaskNotFound
from shu.workflow_store import WorkflowStore


class TaskReadConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.engine = TaskEngine(Path(self.temporary.name))
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        self.tasks = [self.store.submit('concurrent task ' + str(index), {})['task_id']
                      for index in range(8)]

    def test_public_active_check_holds_lock_for_both_identity_and_lifecycle_reads(self):
        database = self.engine._db
        engine = self.engine
        checked = []

        class CheckedConnection:
            def execute(self, sql, *args):
                if sql.startswith(('SELECT * FROM tasks WHERE id=',
                                   'SELECT * FROM workflow_lifecycle WHERE task_id=')):
                    if not engine._lock._is_owned():
                        raise AssertionError('shared SQLite task read outside engine lock')
                    checked.append(sql)
                return database.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(database, name)

        guarded = CheckedConnection()
        self.engine._db = self.store._db = self.store.management._db = guarded
        self.store.management.require_active(self.tasks[0])
        self.engine._task_row(self.tasks[0])
        self.assertEqual(len(checked), 3)

    def test_eight_readers_keep_task_identity_and_lifecycle_consistent(self):
        barrier = threading.Barrier(8)

        def read_tasks(worker):
            barrier.wait(timeout=10)
            for iteration in range(2000):
                task_id = self.tasks[(iteration + worker) % len(self.tasks)]
                if worker % 2:
                    self.store.management.require_active(task_id)
                else:
                    self.assertEqual(self.engine.get_task(task_id)['id'], task_id)
                    self.assertEqual(len(self.engine.list_tasks()), len(self.tasks))

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(read_tasks, range(8)))
        self.assertEqual({task['id'] for task in self.engine.list_tasks()}, set(self.tasks))

    def test_real_missing_and_recycled_tasks_remain_rejected(self):
        with self.assertRaises(TaskNotFound):
            self.store.management.require_active('missing-task')
        self.store.management.trash(self.tasks[0])
        with self.assertRaises(ActionRejected):
            self.store.management.require_active(self.tasks[0])


if __name__ == '__main__':
    unittest.main()

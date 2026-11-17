"""Upgrade persisted cleanup progress before the first task panel read."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from shu.core import TaskEngine
from shu.memory import ConversationMemory
from shu.task_cleanup import TaskCleanup
from shu.task_dialogue import TaskDialogue
from shu.workflow_store import WorkflowStore


class CleanupSchemaUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.engine = TaskEngine(self.path)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)
        self.memory = ConversationMemory(self.path)
        self.addCleanup(self.memory.close)
        self.controller = SimpleNamespace(
            execution=SimpleNamespace(store=self.store), memory=self.memory,
            data_dir=self.path, _task_id=None, _closed=False)

    def legacy_jobs(self, *, with_stage=False):
        with self.engine._transaction():
            self.engine._db.execute(
                'CREATE TABLE workflow_cleanup_jobs(task_id TEXT PRIMARY KEY, '
                'operation_id TEXT NOT NULL, completed TEXT NOT NULL, '
                'manifest TEXT NOT NULL, error TEXT NOT NULL DEFAULT "")')
            if with_stage:
                self.engine._db.execute(
                    'ALTER TABLE workflow_cleanup_jobs ADD COLUMN stage TEXT NOT NULL DEFAULT ""')
            self.engine._db.execute(
                'INSERT INTO workflow_cleanup_jobs(task_id,operation_id,completed,manifest,error) '
                'VALUES(?,?,?,?,?)', ('old', 'operation', '["checkpoint"]', '{"files":[]}', 'keep-error'))
        return dict(self.engine._db.execute('SELECT * FROM workflow_cleanup_jobs').fetchone())

    def dialogue(self):
        value = TaskDialogue(self.controller)
        self.addCleanup(value.close)
        return value

    def test_first_state_read_upgrades_legacy_jobs_without_constructing_cleanup_worker(self):
        before = self.legacy_jobs()
        state = self.dialogue().state()
        self.assertEqual(state['cleanup'][0]['task_id'], 'old')
        self.assertEqual(state['cleanup'][0]['stage'], '')
        self.assertEqual(state['cleanup'][0]['updated_at'], '')
        after = dict(self.engine._db.execute('SELECT * FROM workflow_cleanup_jobs').fetchone())
        self.assertEqual({key: after[key] for key in before}, before)
        self.assertEqual(json.loads(after['completed']), ['checkpoint'])
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM workflow_operations').fetchone()[0], 0)

    def test_partial_upgrade_preserves_existing_stage(self):
        self.legacy_jobs(with_stage=True)
        self.engine._db.execute('UPDATE workflow_cleanup_jobs SET stage="paused"')
        state = self.dialogue().state()
        self.assertEqual(state['cleanup'][0]['stage'], 'paused')
        self.assertEqual(state['cleanup'][0]['updated_at'], '')

    def test_fresh_and_repeated_initialization_are_idempotent(self):
        self.assertEqual(self.dialogue().state()['cleanup'], [])
        TaskCleanup(self.store)
        before = [tuple(row) for row in self.engine._db.execute('PRAGMA table_info(workflow_cleanup_jobs)')]
        self.assertEqual(self.dialogue().state()['cleanup'], [])
        after = [tuple(row) for row in self.engine._db.execute('PRAGMA table_info(workflow_cleanup_jobs)')]
        self.assertEqual(after, before)

    def test_upgrade_also_preserves_registered_legacy_files(self):
        self.legacy_jobs()
        self.engine._db.execute('CREATE TABLE workflow_cleanup_files('
                                'task_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(task_id,path))')
        self.engine._db.execute('INSERT INTO workflow_cleanup_files VALUES(?,?)', ('old', 'private-path'))
        self.dialogue().state()
        row = dict(self.engine._db.execute('SELECT * FROM workflow_cleanup_files').fetchone())
        self.assertEqual(row, {'task_id': 'old', 'path': 'private-path', 'sha256': None})

    def test_two_connections_can_upgrade_the_same_old_database(self):
        before = self.legacy_jobs()
        other = TaskEngine(self.path)
        self.addCleanup(other.close)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(TaskCleanup.ensure_schema, engine)
                       for engine in (self.engine, other)]
            for future in futures:
                future.result(timeout=5)
        for engine in (self.engine, other):
            row = dict(engine._db.execute('SELECT * FROM workflow_cleanup_jobs').fetchone())
            self.assertEqual({key: row[key] for key in before}, before)
            self.assertIn('stage', row)
            self.assertIn('updated_at', row)

    def test_upgraded_columns_and_progress_survive_reopening(self):
        before = self.legacy_jobs()
        self.dialogue().state()
        reopened = TaskEngine(self.path)
        self.addCleanup(reopened.close)
        row = dict(reopened._db.execute('SELECT * FROM workflow_cleanup_jobs').fetchone())
        self.assertEqual({key: row[key] for key in before}, before)
        self.assertEqual(row['stage'], '')
        self.assertEqual(row['updated_at'], '')


if __name__ == '__main__':
    unittest.main()

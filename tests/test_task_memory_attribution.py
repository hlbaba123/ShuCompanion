"""Crash recovery and deletion of explicitly owned transcript projections."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from shu.core import TaskEngine
from shu.maa_facts import RosterStore
from shu.memory import ConversationMemory
from shu.workflow_store import WorkflowStore


class TaskMemoryAttributionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.engine = TaskEngine(self.path)
        self.addCleanup(self.engine.close)
        self.store = WorkflowStore(self.engine)

    def memory(self):
        memory = ConversationMemory(self.path, task_visible=lambda task:
            self.store.management.lifecycle(task)['state'] == 'active')
        self.addCleanup(memory.close)
        return memory

    def test_request_only_append_recovers_after_restart_then_trash_and_delete_are_scoped(self):
        task = self.store.submit('remove synthetic task', {})['task_id']
        other = self.store.submit('keep synthetic task', {})['task_id']
        RosterStore(self.store)
        self.engine._db.execute('INSERT INTO workflow_roster_batches VALUES(?,?,?,?,?,?,?)',
            ('box', 'profile', 1, 'hash', 'input', None, '{"operators":[]}'))
        self.engine._db.execute('INSERT INTO workflow_roster_current VALUES(?,?)', ('profile', 'box'))
        box_before = [tuple(row) for row in self.engine._db.execute('SELECT * FROM workflow_roster_batches')]
        memory = self.memory()
        legacy = memory.append('system', 'legacy unbound record')
        chat_user = memory.append('user', 'ordinary chat', request_id='chat')
        chat_reply = memory.append('assistant', 'ordinary chat reply', request_id='chat')
        keep = memory.append('system', 'other task', request_id='keep', task_id=other)
        private = memory.append('user', 'accepted task text', request_id='remove')
        reply = memory.append('assistant', 'task reply', request_id='remove')
        memory.close()  # Crash after workflow admission, before in-memory message IDs bind.
        memory = self.memory()
        memory.bind_request('remove', task, [])
        self.store.management.trash(task)
        expected = [legacy['id'], chat_user['id'], chat_reply['id'], keep['id']]
        self.assertEqual([row['id'] for row in memory.load_messages()], expected)
        self.assertEqual(memory.model_messages(), [
            {'role': 'user', 'content': 'ordinary chat'},
            {'role': 'assistant', 'content': 'ordinary chat reply'},
        ])
        self.store.management.restore(task)
        self.assertEqual([row['id'] for row in memory.load_messages()][-2:],
                         [private['id'], reply['id']])
        self.store.management.trash(task)
        self.assertEqual(memory.delete_task(task), 2)
        self.assertEqual(memory.delete_task(task), 0)
        self.assertEqual([row['id'] for row in memory.load_messages()], expected)
        with closing(sqlite3.connect(memory.database_path)) as db:
            self.assertEqual(db.execute('SELECT request_id, message_id FROM conversation_request_links ORDER BY message_id').fetchall(),
                [('chat', chat_user['id']), ('chat', chat_reply['id']), ('keep', keep['id'])])
        self.assertEqual([tuple(row) for row in self.engine._db.execute('SELECT * FROM workflow_roster_batches')], box_before)
        self.assertEqual(self.engine._db.execute('SELECT * FROM workflow_roster_current').fetchall()[0]['batch_id'], 'box')

    def test_sync_binds_trashed_workflow_rows_but_never_infers_target_or_prose(self):
        task = self.store.submit('same words', {})['task_id']
        memory = self.memory()
        memory.append('user', 'same words', request_id='routed')
        memory.append('assistant', 'private reply', request_id='routed')
        pending = memory.append('user', 'same words', request_id='pending')
        legacy = memory.append('system', 'same words')
        self.store.management.accept_message('routed', 'same words')
        self.store.management.finish_message('routed', task_id=task)
        self.store.management.accept_message('pending', 'same words', mode='task', target_task_id=task)
        self.store.management.trash(task)
        memory.close()
        memory = self.memory()
        memory.sync_request_bindings(self.store.management.messages())
        self.assertEqual([row['id'] for row in memory.load_messages()], [pending['id'], legacy['id']])
        self.assertEqual(memory.model_messages(), [])
        self.assertEqual(memory.delete_task(task), 2)
        self.assertEqual([row['id'] for row in memory.load_messages()], [pending['id'], legacy['id']])

    def test_visible_limit_is_applied_after_task_visibility_filter(self):
        task = self.store.submit('hidden', {})['task_id']
        memory = self.memory()
        earlier = memory.append('user', 'visible earlier')
        latest = memory.append('assistant', 'visible latest')
        for number in range(4):
            memory.append('system', f'hidden {number}', request_id='hidden', task_id=task)
        self.store.management.trash(task)
        self.assertEqual([row['id'] for row in memory.load_messages(2)], [earlier['id'], latest['id']])

    def test_request_and_message_ownership_are_immutable_and_deleted_requests_stay_fenced(self):
        memory = self.memory()
        first = self.store.submit('first', {})['task_id']
        second = self.store.submit('second', {})['task_id']
        original = memory.append('user', 'first', request_id='request')
        unrelated = memory.append('user', 'second', request_id='other')
        memory.bind_request('request', first, [])
        for callback in (
            lambda: memory.bind_request('request', second, []),
            lambda: memory.bind_request('request', second, [unrelated['id']]),
            lambda: memory.bind_request('new', first, [original['id']]),
            lambda: memory.bind_request('request', first, [unrelated['id']]),
            lambda: memory.append('assistant', 'cannot move request', request_id='request', task_id=second),
        ):
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                callback()
        reply = memory.append('assistant', 'inherits request owner', request_id='request')
        memory.close()
        memory = self.memory()
        self.assertEqual(memory.delete_task(first), 2)
        for callback in (
            lambda: memory.append('assistant', 'late reply', request_id='request'),
            lambda: memory.append('assistant', 'late move', request_id='request', task_id=second),
            lambda: memory.bind_request('request', second, []),
        ):
            with self.subTest(callback=callback), self.assertRaises(ValueError):
                callback()
        self.assertEqual([row['id'] for row in memory.load_messages()], [unrelated['id']])
        self.assertGreater(reply['id'], original['id'])

    def test_legacy_task_links_are_migrated_without_guessing_unbound_records(self):
        memory = self.memory()
        task = self.store.submit('legacy', {})['task_id']
        owned = memory.append('user', 'owned legacy text')
        unbound = memory.append('user', 'same owned legacy text')
        memory._db.execute('INSERT INTO conversation_task_links VALUES(?,?,?)', (owned['id'], 'legacy', task))
        memory.close()
        memory = self.memory()
        memory.bind_request('legacy', task, [])
        self.assertEqual(memory.delete_task(task), 1)
        self.assertEqual([row['id'] for row in memory.load_messages()], [unbound['id']])

    def test_clear_removes_request_links_and_keeps_message_ids_monotonic(self):
        memory = self.memory()
        original = memory.append('user', 'pending', request_id='pending')
        memory.clear()
        memory.bind_request('pending', self.store.submit('no guessed messages', {})['task_id'], [])
        self.assertEqual(memory.load_messages(), [])
        with closing(sqlite3.connect(memory.database_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_request_links').fetchone()[0], 0)
        self.assertGreater(memory.append('user', 'new chat')['id'], original['id'])

    def test_delete_resolves_earlier_request_text_and_sync_retries_after_partial_purge(self):
        task = self.store.submit('remove', {})['task_id']
        memory = self.memory()
        memory.append('user', 'before task routing', request_id='owned')
        memory.append('assistant', 'task known', request_id='owned', task_id=task)
        self.store.management.trash(task)
        self.assertEqual(memory.load_messages(), [])
        self.assertEqual(memory.model_messages(), [])
        self.assertEqual(memory.delete_task(task), 2)
        memory.close()
        memory = self.memory()
        # Workflow rows can remain after the memory projection was purged.
        memory.sync_request_bindings([{'request_id': 'owned', 'task_id': task}])
        self.assertEqual(memory.load_messages(), [])
        with self.assertRaises(ValueError):
            memory.append('assistant', 'late write', request_id='owned')
        # Recover another explicitly admitted request left by an older cleanup.
        memory.append('user', 'interrupted routing', request_id='recover')
        keep = memory.append('user', 'ordinary chat', request_id='chat')
        memory.sync_request_bindings([{'request_id': 'recover', 'task_id': task}])
        self.assertEqual([row['id'] for row in memory.load_messages()], [keep['id']])
        with self.assertRaises(ValueError):
            memory.append('assistant', 'late recovered reply', request_id='recover')


if __name__ == '__main__':
    unittest.main()

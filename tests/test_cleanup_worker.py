"""Isolated persistence + Qt heartbeat; never touches a native app or user data."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal
from shu.core import TaskEngine
from shu.memory import ConversationMemory
from shu.runtime import RuntimeStore
from shu.workflow_store import WorkflowStore
from shu.task_cleanup import TaskCleanup
from shu.task_dialogue import TaskDialogue

APP=QCoreApplication.instance() or QCoreApplication([])

class Signals(QObject):
    changed=Signal()
    cleanup_progress=Signal(dict)

class CleanupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.path=Path(self.folder.name)
        self.engine=TaskEngine(self.path)
        self.store=WorkflowStore(self.engine)
        self.task=self.store.submit('private cleanup',{})['task_id']
        self.store.management.trash(self.task)
        self.c=Signals();self.c.memory=ConversationMemory(self.path);self.c.runtime=RuntimeStore(self.path)
        self.c.execution=SimpleNamespace(store=self.store,_job=None,directory=self.path,
            graph_lock=threading.RLock())
        self.c._task_id=None;self.c._closed=False
        self.dialogue=TaskDialogue(self.c)

    def tearDown(self):
        if callable(getattr(self.dialogue,'close',None)):self.dialogue.close()
        self.c.memory.close();self.c.runtime.close();self.engine.close();self.folder.cleanup()

    def test_slow_inventory_keeps_qt_heartbeat_and_other_task_reads_responsive(self):
        start=getattr(self.dialogue,'start_purge',None)
        self.assertTrue(callable(start),'cleanup requires an owned background submission API')
        entered=threading.Event();release=threading.Event();ticks=[]
        original=TaskCleanup._file_manifest
        def slow(cleanup,*args,**kwargs):
            entered.set();release.wait(2)
            return original(cleanup,*args,**kwargs)
        timer=QTimer();timer.setInterval(5);timer.timeout.connect(lambda:ticks.append(1));timer.start()
        self.addCleanup(timer.stop);self.addCleanup(release.set)
        with patch.object(TaskCleanup,'_file_manifest',slow):
            before=time.monotonic();result=start(self.task)
            self.assertLess(time.monotonic()-before,.15)
            self.assertEqual(result['status'],'queued');self.assertTrue(entered.wait(1))
            deadline=time.monotonic()+.1
            while time.monotonic()<deadline:
                APP.processEvents();time.sleep(.002)
            self.assertGreater(len(ticks),5)
            before=time.monotonic();self.store.management.list_tasks()
            self.assertLess(time.monotonic()-before,.08)
            job=self.engine._db.execute('SELECT * FROM workflow_cleanup_jobs WHERE task_id=?',(self.task,)).fetchone()
            self.assertEqual(job['stage'],'manifest');self.assertEqual(job['manifest'],'{}')
            release.set();self.dialogue.close()
        self.assertFalse(any(t.name.startswith('shu-task-cleanup') and t.is_alive() for t in threading.enumerate()))

    def test_shutdown_pauses_at_stage_boundary_then_restart_resumes_without_late_writes(self):
        start=getattr(self.dialogue,'start_purge',None)
        self.assertTrue(callable(start),'cleanup worker must stop and join before database close')
        entered=threading.Event();release=threading.Event();original=TaskCleanup._file_manifest
        def slow(cleanup,*args,**kwargs):
            entered.set();release.wait(2)
            return original(cleanup,*args,**kwargs)
        with patch.object(TaskCleanup,'_file_manifest',slow):
            start(self.task);self.assertTrue(entered.wait(1))
            closer=threading.Thread(target=self.dialogue.close);closer.start()
            time.sleep(.03);self.assertTrue(closer.is_alive(),'close must join, never orphan IO')
            release.set();closer.join(2);self.assertFalse(closer.is_alive())
        self.assertEqual(self.store.management.lifecycle(self.task)['state'],'purge_pending')
        row=self.engine._db.execute('SELECT stage FROM workflow_cleanup_jobs WHERE task_id=?',(self.task,)).fetchone()
        self.assertEqual(row['stage'],'paused')
        self.dialogue=TaskDialogue(self.c);self.dialogue.resume_cleanup()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and self.store.management.lifecycle(self.task)['state']!='purged':
            APP.processEvents();time.sleep(.005)
        self.assertEqual(self.store.management.lifecycle(self.task)['state'],'purged')


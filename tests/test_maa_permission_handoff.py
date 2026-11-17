"""Session permissions must be explicit and resume the selected import task."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication
from shu.controller import SessionController


class MaaPermissionHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c=SessionController(Path(self.temp.name),Path(self.temp.name)/'data')
        self.addCleanup(self.c.close)
        # No real workflow is created by these controller boundary tests.
        self.addCleanup(lambda:setattr(self.c,'_task_id',None))
        self.state={'status':'needs_input','contract':{
            'goal':'只查询并导入 TL-6 作业，不开始作战','task_spec':{'kind':'import'},
            'allow_start':False,'attempt_limit':0}}
        self.c.execution.store.management.require_active=Mock()
        self.c.execution.store.current=Mock(side_effect=lambda task:self.state)
        self.c.execution.resume=Mock(return_value={'accepted':True,'task_id':'task-a'})

    def test_ordinary_continue_cannot_grant_permissions_or_restart_research(self):
        self.assertFalse(self.c.game_input_allowed)
        result=self.c.continue_task('task-a')
        self.assertEqual(result['code'],'maa_permission_required')
        self.assertFalse(self.c.game_input_allowed)
        self.c.execution.resume.assert_not_called()

    def test_explicit_authorization_resumes_same_task_and_is_not_persisted(self):
        self.c.authorize_maa_import('task-a')
        self.assertTrue(self.c.game_input_allowed)
        self.assertTrue(self.c.screen_capture_allowed)
        self.c.execution.resume.assert_called_once_with('task-a')
        self.assertEqual(self.c._task_id,'task-a')
        state=self.c.task_management_state()
        self.assertEqual(state['permissions'],{'screen_capture':True,'maa_input':True})
        other=SessionController(Path(self.temp.name),Path(self.temp.name)/'restarted')
        try:self.assertFalse(other.game_input_allowed)
        finally:other.close()

    def test_deleted_or_changed_task_does_not_receive_permission(self):
        self.c.execution.store.management.require_active.side_effect=ValueError('in recycle bin')
        with self.assertRaises(ValueError):self.c.authorize_maa_import('task-a')
        self.assertFalse(self.c.game_input_allowed)
        self.c.execution.resume.assert_not_called()
        self.c.execution.store.management.require_active.side_effect=None
        self.state['contract']['allow_start']=True
        with self.assertRaises(ValueError):self.c.authorize_maa_import('task-a')
        self.assertFalse(self.c.game_input_allowed)

    def test_capture_preference_failure_cannot_continue_or_enable_input(self):
        with patch.object(self.c,'set_capture_permission'):
            with self.assertRaises(ValueError):self.c.authorize_maa_import('task-a')
        self.assertFalse(self.c.game_input_allowed)
        self.c.execution.resume.assert_not_called()

    def test_legacy_draft_can_reach_existing_coordinator_adoption_without_grant(self):
        from shu.core import TaskNotFound
        self.c.execution.store.current.side_effect=TaskNotFound('legacy draft')
        self.c.continue_task('legacy-draft')
        self.c.execution.resume.assert_called_once_with('legacy-draft')
        self.assertFalse(self.c.game_input_allowed)


if __name__=='__main__':unittest.main()

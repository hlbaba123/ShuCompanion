"""Product coordinator uses real SQLite authority; model/GUI are offline doubles."""
from pathlib import Path
import hashlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

from shu.core import ActionRejected, TaskEngine
from shu.providers import ProviderConfig, ProviderError
from shu.runtime import RuntimeStore
from shu.workflow_store import WorkflowStore


APP = QCoreApplication.instance() or QCoreApplication([])


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('worker did not reach expected state')


class ReadOnlyWindows:
    """Coordinator must never bind a game or enable its input."""
    def __init__(self):
        self.stopped = False

    def set_stop(self):
        self.stopped = True

    def bind(self, *_):
        raise AssertionError('MAA coordinator bound a game window')

    def set_input_enabled(self, *_):
        raise AssertionError('MAA coordinator enabled game input')


class Roles:
    def __init__(self, store, directory, **kwargs):
        self.store = store
        self.handlers = {'leader': lambda context, cancel: {}}
        self.closed = False
        self.stops = []

    def stop(self, task_id, *, control_seq=None):
        state = self.store.current(task_id)
        if state['writes_enabled'] or state['control_seq'] != control_seq:
            raise AssertionError('stop was dispatched before durable revocation')
        self.stops.append(task_id)
        return {'status': 'unknown', 'external_stop_confirmed': False,
                'summary': '外部停止尚未核实'}

    def close(self):
        self.closed = True


class OfflineWorkflows:
    def __init__(self, *, blocking=False, result=None, error=None, close_error=False):
        self.blocking = blocking
        self.result = result or {'status': 'needs_input', 'summary': '需要真实导入证据'}
        self.error = error
        self.close_error = close_error
        self.entered, self.release = threading.Event(), threading.Event()
        self.runs, self.instances = [], []

    def __call__(self, store, directory, handlers, *, emit, cancel_event):
        owner = self

        class Workflow:
            def run(self, task_id):
                owner.runs.append((task_id, threading.get_ident()))
                owner.entered.set()
                if owner.blocking:
                    owner.release.wait(3)
                if owner.error:
                    raise owner.error
                return dict(owner.result)

            def close(self):
                self.closed = True
                if owner.close_error:
                    raise OSError('fixture cleanup failed')

        value = Workflow()
        self.instances.append(value)
        return value


class MaaCoordinatorTests(unittest.TestCase):
    def test_typed_import_also_delivers_source_query_codes_and_gaps(self):
        coordinator = self.make_coordinator()
        source = self.store.submit('查询整个活动', {'current_deliverable':'copilot_codes'})
        original = {'status':'needs_input', 'code':'queue_pending', 'summary':'导入尚待核对。'}
        self.workflows.result = original
        delivery = {'items':[{'stage_code':'IW-1','primary_code':'prts://42'},
                             {'stage_code':'IW-2','primary_code':None}],
                    'complete':False, 'summary':'IW-1：prts://42\nIW-2：尚无合格主选。'}
        results = []
        coordinator.finished.connect(results.append)
        with patch.object(coordinator, 'research_output', return_value=delivery) as read:
            coordinator.submit('查询整个活动，只导入 IW-1', {
                'current_deliverable':'maa_import', 'source_task_id':source['task_id'],
                'import_scope':{'kind':'stages', 'stage_codes':['IW-1'], 'difficulty':1},
                'task_spec':{'kind':'import'}})
            self.workflows.release.set()
            wait_for(lambda:not coordinator.active)
            APP.processEvents()
        self.assertEqual(results[0]['status'], original['status'])
        self.assertEqual(results[0]['code'], original['code'])
        self.assertTrue(results[0]['summary'].startswith(original['summary']))
        self.assertIn(delivery['summary'], results[0]['summary'])
        self.assertEqual(results[0]['query_delivery'], delivery)
        read.assert_called_once_with(source['task_id'])

    def test_typed_import_question_does_not_append_previous_query_output(self):
        coordinator = self.make_coordinator()
        original = {'status':'needs_input', 'waiting_kind':'business',
                    'summary':'请选择要导入的关卡和难度。'}
        self.workflows.result = original
        results = []
        coordinator.finished.connect(results.append)
        with patch.object(coordinator, 'research_output') as read:
            coordinator.submit('查询活动，只导入第一关', {
                'current_deliverable':'maa_import', 'import_scope':{'kind':'ordinal', 'ordinal':1},
                'task_spec':{'kind':'import'}})
            self.workflows.release.set()
            wait_for(lambda:not coordinator.active)
            APP.processEvents()
        self.assertEqual(results[0]['summary'], original['summary'])
        self.assertNotIn('query_delivery', results[0])
        read.assert_not_called()

    def test_independent_typed_task_queues_without_cancelling_old_task(self):
        coordinator = self.make_coordinator()
        first = coordinator.submit('先查甲活动', {'relation': 'new', 'current_deliverable': 'stage_catalog'})
        self.assertTrue(self.workflows.entered.wait(1))
        second = coordinator.submit('现在查乙活动', {'relation': 'new', 'current_deliverable': 'stage_catalog'})
        self.assertNotEqual(first['task_id'], second['task_id'])
        self.assertEqual(self.store.current(first['task_id'])['status'], 'running')

    def test_typed_modify_selects_named_task_not_unrelated_current_task(self):
        coordinator = self.make_coordinator()
        first = self.store.submit('甲活动', {'current_deliverable': 'stage_catalog'})
        self.store.control(first['task_id'], 'pause')
        other = self.store.submit('乙活动', {'current_deliverable': 'stage_catalog'})
        coordinator.current_task_id = other['task_id']
        result = coordinator.submit('修改甲', {'relation': 'modify',
            'selected_task_id': first['task_id'], 'current_deliverable': 'stage_catalog'}, modify=True)
        self.assertEqual(result['task_id'], first['task_id'])
        self.assertEqual(self.store.current(other['task_id'])['contract']['current_deliverable'], 'stage_catalog')

    def test_actual_graph_cleanup_does_not_cancel_the_coordinators_completion(self):
        from shu.maa_graph import MaaWorkflow
        coordinator=self.make_coordinator()
        coordinator.workflow_factory=MaaWorkflow
        results=[];coordinator.finished.connect(results.append)
        outcome=coordinator.submit('只读规划，缺证据时说明')
        wait_for(lambda:not coordinator.active)
        APP.processEvents()
        self.assertEqual(self.engine.get_task(outcome['task_id'])['status'],'needs_input')
        self.assertEqual(len(results),1)
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.engine = TaskEngine(self.directory)
        self.runtime = RuntimeStore(self.directory)
        self.store = WorkflowStore(self.engine)
        self.windows = ReadOnlyWindows()
        self.workflows = OfflineWorkflows(blocking=True)
        self.roles = []
        self.permissions = [True, True]
        self.coordinator = None

    def make_coordinator(self, **kwargs):
        from shu.maa_coordinator import MaaCoordinator

        def roles_factory(*args, **options):
            value = Roles(*args, **options)
            self.roles.append(value)
            return value

        self.coordinator = MaaCoordinator(
            self.engine, self.windows, self.runtime, directory=self.directory,
            role_config=ProviderConfig(), task_config=ProviderConfig(),
            permissions=lambda: tuple(self.permissions), store=self.store,
            role_factory=roles_factory, workflow_factory=self.workflows, **kwargs,
        )
        return self.coordinator

    def tearDown(self):
        self.workflows.release.set()
        if self.coordinator:
            self.coordinator.close()
        self.runtime.close()
        self.engine.close()
        self.temp.cleanup()

    def test_submit_runs_in_background_with_import_only_durable_contract(self):
        coordinator = self.make_coordinator()
        result = coordinator.submit('查找并导入作业', {'goal': '查找并导入作业',
                                                 'allow_start': True, 'attempt_limit': 3})
        self.assertTrue(result['accepted'])
        self.assertTrue(self.workflows.entered.wait(1))
        state = self.store.current(result['task_id'])
        self.assertFalse(state['contract']['allow_start'])
        self.assertEqual(state['contract']['attempt_limit'], 0)
        self.assertNotEqual(self.workflows.runs[0][1], threading.get_ident())
        self.assertTrue(coordinator.active)

    def test_runtime_contract_cannot_override_sqlite_authority(self):
        coordinator = self.make_coordinator()
        result = coordinator.submit('导入', {'goal': '可信目标'})
        self.runtime.save(result['task_id'], 100, phase='completed',
                          contract={'goal': '伪目标', 'allow_start': True})
        self.assertEqual(coordinator.contract()['goal'], '可信目标')
        self.assertFalse(coordinator.contract()['allow_start'])

    def test_progress_projects_only_public_current_task_facts(self):
        coordinator = self.make_coordinator()
        events = []
        coordinator.event.connect(lambda kind, summary, facts: events.append((kind, summary, facts)))
        result = coordinator.submit('导入')
        self.assertTrue(self.workflows.entered.wait(1))
        job = coordinator._job
        coordinator._event(job, 'tool_finished', {'role': 'research', 'tool': 'research_search',
            'status': 'completed', 'summary': '资料查询返回。 sk-private-secret-value',
            'reasoning_content': 'HIDDEN REASONING', 'result': {'full': 'PRIVATE PAYLOAD'}})
        wait_for(lambda: any(kind == 'tool_finished' for kind, _, _ in events))
        event = next(facts for kind, _, facts in events if kind == 'tool_finished')
        self.assertEqual(event['task_id'], result['task_id'])
        self.assertEqual(event['epoch'], job.epoch)
        self.assertNotIn('reasoning_content', event)
        self.assertNotIn('result', event)
        self.assertNotIn('sk-private', str(event))
        count = len(events)
        self.store.control(result['task_id'], 'pause')
        coordinator._event(job, 'tool_finished', {'role': 'research', 'status': 'completed'})
        APP.processEvents()
        self.assertEqual(len(events), count)

    def test_needs_input_activity_uses_revoked_epoch_visible_to_controller(self):
        coordinator = self.make_coordinator()
        events = []
        coordinator.event.connect(lambda kind, summary, facts: events.append((kind, facts)))
        result = coordinator.submit('导入')
        self.assertTrue(self.workflows.entered.wait(1))
        self.workflows.release.set()
        wait_for(lambda: any(kind == 'task_waiting' for kind, _ in events))
        event = next(facts for kind, facts in events if kind == 'task_waiting')
        self.assertEqual(event['epoch'], self.engine.get_task(result['task_id'])['version'])
        self.assertEqual(event['status'], 'needs_input')

    def test_stop_revokes_while_graph_is_blocked_and_reports_unconfirmed_external_stop(self):
        coordinator = self.make_coordinator()
        result = coordinator.submit('导入')
        self.assertTrue(self.workflows.entered.wait(1))
        before = time.monotonic()
        stopped = coordinator.stop()
        self.assertLess(time.monotonic() - before, .25)
        state = self.store.current(result['task_id'])
        self.assertFalse(state['writes_enabled'])
        self.assertEqual(state['status'], 'cancelled')
        self.assertFalse(stopped['external_stop_confirmed'])
        wait_for(lambda: bool(self.roles[0].stops))
        self.assertTrue(coordinator.active, 'blocking model must remain supervised until it exits')
        self.workflows.release.set()
        wait_for(lambda: not coordinator.active)
        self.assertEqual(self.engine.get_task(result['task_id'])['status'], 'cancelled')

    def test_pause_then_resume_preserves_contract_and_gets_new_epoch(self):
        coordinator = self.make_coordinator()
        result = coordinator.submit('导入', {'goal': '原目标', 'constraints': ['禁止开始']})
        self.assertTrue(self.workflows.entered.wait(1))
        original = self.store.current(result['task_id'])
        coordinator.pause()
        paused = self.store.current(result['task_id'])
        self.assertFalse(paused['writes_enabled'])
        self.workflows.release.set()
        wait_for(lambda: not coordinator.active)
        resumed = coordinator.resume()
        wait_for(lambda: len(self.workflows.runs) == 2)
        current = self.store.current(result['task_id'])
        self.assertTrue(resumed['accepted'])
        self.assertGreater(current['epoch'], original['epoch'])
        self.assertEqual(current['revision'], original['revision'])
        self.assertEqual(current['contract']['constraints'], ['禁止开始'])

    def test_restart_persistently_revokes_even_draft_authority_without_model_call(self):
        previous = self.store.submit('导入', {'goal': '恢复原目标'})
        coordinator = self.make_coordinator()
        current = self.store.current(previous['task_id'])
        self.assertFalse(current['writes_enabled'])
        self.assertEqual(current['status'], 'paused')
        raw = self.engine._db.execute('SELECT writes_enabled FROM workflow_tasks WHERE task_id=?',
                                      (previous['task_id'],)).fetchone()[0]
        self.assertEqual(raw, 0)
        self.assertEqual(self.workflows.runs, [])
        self.assertEqual(coordinator.contract(previous['task_id'])['goal'], '恢复原目标')

    def test_modify_fences_old_run_and_serializes_replacement(self):
        coordinator = self.make_coordinator()
        first = coordinator.submit('旧目标', {'goal': '旧目标'})
        self.assertTrue(self.workflows.entered.wait(1))
        second = coordinator.submit('新目标', {'goal': '新目标'}, modify=True)
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(len(self.workflows.runs), 1)
        self.assertEqual(coordinator.contract()['goal'], '新目标')
        self.workflows.release.set()
        wait_for(lambda: len(self.workflows.runs) == 2)
        wait_for(lambda: not coordinator.active)
        self.assertEqual(self.store.current(first['task_id'])['revision'], 2)

    def test_workflow_failure_is_truthful_needs_input_without_legacy_fallback(self):
        self.workflows = OfflineWorkflows(error=RuntimeError('fixture workflow unavailable'))
        coordinator = self.make_coordinator()
        results = []
        coordinator.finished.connect(results.append)
        outcome = coordinator.submit('导入')
        wait_for(lambda: bool(results))
        self.assertEqual(results[-1]['status'], 'needs_input')
        self.assertIn('fixture workflow unavailable', results[-1]['summary'])
        self.assertEqual(self.engine.get_task(outcome['task_id'])['status'], 'needs_input')
        self.assertFalse(self.store.current(outcome['task_id'])['writes_enabled'])

    def test_import_failure_preserves_error_instead_of_replacing_it_with_research_summary(self):
        self.workflows = OfflineWorkflows(error=RuntimeError('fixture import receipt failure'))
        coordinator = self.make_coordinator()
        results=[]
        coordinator.finished.connect(results.append)
        coordinator.submit('导入 TL-2 的作业', {'goal':'导入 TL-2 的作业',
            'current_deliverable':'maa_import','task_spec':{'kind':'import','stage_code':'TL-2'}})
        wait_for(lambda: bool(results))
        self.assertIn('fixture import receipt failure', results[-1]['summary'])

    def test_read_only_planning_can_resume_without_gui_input_permission(self):
        previous = self.store.submit('导入', {'goal': '原目标'})
        coordinator = self.make_coordinator()
        self.permissions[1] = False
        outcome = coordinator.resume(previous['task_id'])
        self.assertTrue(outcome['accepted'])
        self.assertTrue(self.workflows.entered.wait(1))
        self.assertFalse(self.permissions[1])

    def test_planning_does_not_require_screen_or_input_and_preserves_requested_goal(self):
        self.permissions[:] = [False,False]
        coordinator = self.make_coordinator()
        goal = '导入 TL-10 后刷六次，不使用理智药'
        outcome = coordinator.submit(goal, {'goal':goal,'constraints':['不使用理智药']})
        self.assertTrue(self.workflows.entered.wait(1))
        contract = coordinator.contract(outcome['task_id'])
        self.assertEqual(contract['goal'],goal)
        self.assertFalse(contract['allow_start'])
        self.assertEqual(contract['attempt_limit'],0)

    def test_import_completion_requires_current_durable_maa_receipt(self):
        state = self.store.submit('导入', {'allow_start': False})
        path = self.directory / 'import-verified.json'
        path.write_text('{"synthetic":"readback"}', encoding='utf-8')
        refs = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        with self.assertRaises(ActionRejected):
            self.store.complete_import_verified(state['task_id'], refs, expected_epoch=state['epoch'])
        dispatch = self.store.ensure_dispatch(state['task_id'], 'maa', 'import:1', {})
        self.store.finish_dispatch(dispatch['dispatch_id'], {'status': 'import_verified',
                                   'battle_start_submitted': False, 'evidence_refs': refs})
        completed = self.store.complete_import_verified(state['task_id'], refs, expected_epoch=state['epoch'])
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['completed_runs'], 0)
        self.assertFalse(self.store.current(state['task_id'])['writes_enabled'])
        self.assertTrue(any(event['type'] == 'maa_import_verified'
                            for event in self.engine.events(state['task_id'])))

    def test_import_completion_preserves_more_than_32_refresh_evidence_refs(self):
        state = self.store.submit('导入', {'allow_start': False})
        refs = []
        for number in range(34):
            path = self.directory / f'visual-readback-{number}.json'
            path.write_text('{"observation":' + str(number) + '}', encoding='utf-8')
            refs.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        dispatch = self.store.ensure_dispatch(state['task_id'], 'maa', 'import:refreshes', {})
        self.store.finish_dispatch(dispatch['dispatch_id'], {'status':'import_verified',
            'battle_start_submitted':False, 'evidence_refs':refs})
        # Every reference remains mandatory, including observations past #32.
        last = Path(refs[-1]['path'])
        original = last.read_bytes()
        last.write_text('{"changed":true}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.complete_import_verified(state['task_id'], refs, expected_epoch=state['epoch'])
        last.write_bytes(original)
        completed = self.store.complete_import_verified(state['task_id'], refs, expected_epoch=state['epoch'])
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['completed_runs'], 0)
        self.assertEqual(self.store.dispatch_result(dispatch['dispatch_id'])['evidence_refs'], refs)

    def test_import_completion_still_rejects_excessive_evidence_refs(self):
        state = self.store.submit('导入', {'allow_start': False})
        path = self.directory / 'bounded-readback.json'
        path.write_text('{}', encoding='utf-8')
        ref = {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        with self.assertRaises(ActionRejected):
            self.store.complete_import_verified(state['task_id'], [ref] * 129,
                                                expected_epoch=state['epoch'])

    def test_import_completion_rejects_modified_or_stale_evidence(self):
        state = self.store.submit('导入', {'allow_start': False})
        path = self.directory / 'readback.json'
        path.write_text('{}', encoding='utf-8')
        refs = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        dispatch = self.store.ensure_dispatch(state['task_id'], 'maa', 'import:1', {})
        self.store.finish_dispatch(dispatch['dispatch_id'], {'status': 'import_verified',
                                   'battle_start_submitted': False, 'evidence_refs': refs})
        path.write_text('{"changed":true}', encoding='utf-8')
        with self.assertRaises((ActionRejected, ValueError)):
            self.store.complete_import_verified(state['task_id'], refs, expected_epoch=state['epoch'])
        path.write_text('{}', encoding='utf-8')
        self.store.control(state['task_id'], 'stop')
        with self.assertRaises(ActionRejected):
            self.store.complete_import_verified(state['task_id'], refs, expected_epoch=state['epoch'])

    def test_coordinator_accepts_verified_import_without_battle_count(self):
        coordinator = self.make_coordinator()
        result = coordinator.submit('导入')
        self.assertTrue(self.workflows.entered.wait(1))
        state = self.store.current(result['task_id'])
        path = self.directory / 'verified-import.json'
        path.write_text('{}', encoding='utf-8')
        receipt = {'status': 'import_verified', 'battle_start_submitted': False,
                   'evidence_refs': [{'path': str(path),
                                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
        dispatch = self.store.ensure_dispatch(result['task_id'], 'maa', 'import:fixture', {})
        self.store.finish_dispatch(dispatch['dispatch_id'], receipt)
        self.workflows.result = {'status': 'import_verified', 'summary': '导入已回读核实',
                                 'role_results': {'maa': receipt}}
        self.workflows.release.set()
        wait_for(lambda: not coordinator.active)
        current = self.engine.get_task(result['task_id'])
        self.assertEqual(current['status'], 'completed')
        self.assertEqual(current['completed_runs'], 0)
        self.assertEqual(self.runtime.load(result['task_id'])['phase'], 'import_verified')

    def test_leader_completion_without_durable_receipt_cannot_complete_product_task(self):
        self.workflows = OfflineWorkflows(result={'status': 'import_verified',
              'role_results': {'maa': {'evidence_refs': [{'path': 'invented', 'sha256': '0' * 64}]}}})
        coordinator = self.make_coordinator()
        outcome = coordinator.submit('导入')
        wait_for(lambda: not coordinator.active)
        self.assertEqual(self.engine.get_task(outcome['task_id'])['status'], 'needs_input')
        self.assertFalse(self.store.current(outcome['task_id'])['writes_enabled'])

    def test_cleanup_failure_still_revokes_writes_and_releases_activity(self):
        self.workflows = OfflineWorkflows(close_error=True)
        coordinator = self.make_coordinator()
        outcome = coordinator.submit('导入')
        wait_for(lambda: not coordinator.active)
        self.assertFalse(self.store.current(outcome['task_id'])['writes_enabled'])
        self.assertEqual(self.engine.get_task(outcome['task_id'])['status'], 'needs_input')

    def test_replacement_waits_for_stop_cleanup_and_does_not_launch_on_timeout(self):
        coordinator = self.make_coordinator()
        coordinator.control_timeout_seconds = .05
        original = coordinator.submit('导入原任务')
        self.assertTrue(self.workflows.entered.wait(1))
        stop_entered, release_stop = threading.Event(), threading.Event()
        def slow_stop(*args, **kwargs):
            stop_entered.set()
            release_stop.wait(2)
            return {'status': 'unknown', 'external_stop_confirmed': False}
        self.roles[0].stop = slow_stop
        try:
            replacement = coordinator.submit('替换导入任务')
            self.assertTrue(stop_entered.wait(1))
            self.workflows.release.set()
            wait_for(lambda: not coordinator.active)
            self.assertEqual(len(self.workflows.runs), 1)
            self.assertFalse(self.store.current(replacement['task_id'])['writes_enabled'])
        finally:
            release_stop.set()

    def test_transport_exception_never_exposes_credentials_in_finished_summary(self):
        self.workflows = OfflineWorkflows(error=RuntimeError('Bearer sk-super-secret-123456'))
        coordinator = self.make_coordinator()
        results = []
        coordinator.finished.connect(results.append)
        coordinator.submit('导入')
        wait_for(lambda: bool(results))
        self.assertNotIn('sk-super-secret', results[-1]['summary'])

    def test_trusted_resume_adopts_pre_migration_draft_goal_without_runtime_authority(self):
        draft = self.engine.submit_goal('导入作业，禁止开始')
        self.runtime.save(draft['id'], draft['version'], phase='draft',
                          contract={'goal': '伪造覆盖', 'allow_start': True})
        coordinator = self.make_coordinator()
        coordinator.current_task_id = draft['id']
        result = coordinator.resume()
        self.assertEqual(result['task_id'], draft['id'])
        self.assertEqual(coordinator.contract()['goal'], '导入作业，禁止开始')
        self.assertFalse(coordinator.contract()['allow_start'])

    def test_stop_does_not_wait_for_import_evidence_hashing(self):
        coordinator = self.make_coordinator()
        outcome = coordinator.submit('导入')
        self.assertTrue(self.workflows.entered.wait(1))
        hashing, release = threading.Event(), threading.Event()
        original = self.store.complete_import_verified

        def delayed(*args, **kwargs):
            hashing.set()
            release.wait(2)
            return original(*args, **kwargs)

        self.store.complete_import_verified = delayed
        self.workflows.result = {'status': 'import_verified', 'role_results': {'maa': {
            'status':'import_verified','battle_start_submitted':False,
            'evidence_refs':[{'path':str(self.directory/'hashing-fixture.json'),'sha256':'0'*64}]}}}
        self.workflows.release.set()
        self.assertTrue(hashing.wait(1))
        before = time.monotonic()
        coordinator.stop()
        elapsed = time.monotonic() - before
        release.set()
        self.assertLess(elapsed, .25)
        self.assertEqual(self.engine.get_task(outcome['task_id'])['status'], 'cancelled')

    def test_close_revokes_active_task_even_if_current_pointer_has_changed(self):
        coordinator = self.make_coordinator()
        outcome = coordinator.submit('导入')
        self.assertTrue(self.workflows.entered.wait(1))
        newer = self.engine.submit_goal('待处理的新草稿')
        coordinator.current_task_id = newer['id']
        self.workflows.release.set()
        coordinator.close()
        self.assertFalse(self.store.current(outcome['task_id'])['writes_enabled'])
        self.assertEqual(self.engine.get_task(outcome['task_id'])['status'], 'paused')

    def test_normal_controller_uses_maa_and_legacy_is_explicit(self):
        from shu.controller import SessionController
        from shu.coordinator import ExecutionCoordinator
        from shu.maa_coordinator import MaaCoordinator
        for legacy, expected in ((False, MaaCoordinator), (True, ExecutionCoordinator)):
            with patch.dict('os.environ', {'DEEPSEEK_API_KEY': ''}):
                controller = SessionController(self.directory, self.directory / str(legacy), legacy_tools=legacy)
            try:
                self.assertIsInstance(controller.execution, expected)
            finally:
                controller.close()

    def test_pause_cancels_pending_intent_before_it_can_create_writable_task(self):
        import json
        from shu.controller import SessionController
        entered, release = threading.Event(), threading.Event()
        class LateModel:
            def complete(self, *args, **kwargs):
                entered.set()
                release.wait(2)
                return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'late-route',
                    'type': 'function', 'function': {'name': 'route_user_request', 'arguments': json.dumps({
                        'intent': 'execute', 'user_request': '导入TL-2', 'goal': '导入TL-2',
                        'constraints': [], 'completion_criteria': [], 'removed_constraints': [], 'reply': '接到'})}}]}
        controller = SessionController(self.directory, self.directory / 'late-intent')
        controller.key_path.write_text('sk-offline-fixture-not-a-key')
        controller.client = controller.intent_client = LateModel()
        controller.persona = controller._new_persona()
        try:
            controller.submit('导入TL-2')
            self.assertTrue(entered.wait(1))
            controller.pause()
            self.assertTrue(controller._active is None or controller._active.cancel.is_set())
            release.set()
            wait_for(lambda: not controller.is_busy)
            self.assertFalse(controller.engine.list_tasks())
        finally:
            release.set()
            controller.close()


if __name__ == '__main__':
    unittest.main()

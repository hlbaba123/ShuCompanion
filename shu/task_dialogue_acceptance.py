"""Real-model, read-only dialogue acceptance in a newly created private profile.

The shared profile is opened read-only, the original Key path is referenced, and
only minimal settings plus the published current Box are copied. No historical
conversation, cached answer, game input or MAA input is used.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

SOURCE_FILES = ('shu/controller.py', 'shu/task_contract.py', 'shu/task_directive.py',
                'shu/task_dialogue.py', 'shu/task_management.py', 'shu/maa_coordinator.py',
                'shu/maa_graph.py', 'shu/task_cleanup.py', 'shu/task_dialogue_acceptance.py')


def source_hashes(project_root):
    """A compiled module cannot prove its identity by hashing adjacent source."""
    if getattr(sys, 'frozen', False):
        return None
    return {name: hashlib.sha256((project_root / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES}


def optional_bytes(path):
    return path.read_bytes() if path.is_file() else None


def write_json(path, value):
    from shu.runtime import _clean
    Path(path).write_text(json.dumps(_clean(value), ensure_ascii=False, indent=2), encoding='utf-8')


def source_box(source):
    """No production constructors/schema migration or production writes."""
    with sqlite3.connect((source / 'tasks.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT b.* FROM workflow_roster_batches b JOIN workflow_roster_current c '
                         'ON c.batch_id=b.batch_id WHERE c.profile_id=?', ('local-default',)).fetchone()
        if row is None:
            raise ValueError('Published user Box is required for this acceptance')
        if hashlib.sha256(row['payload'].encode('utf-8')).hexdigest() != row['content_hash']:
            raise ValueError('Published user Box failed its stored hash')
        return json.loads(row['payload']), row['content_hash']


class Acceptance:
    def __init__(self, output, timeout, *, project_root, resource_root, key_path,
                 source_data, version):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QFont, QFontDatabase
        from shu.controller import SessionController
        from shu.companion_ui import CompanionShell
        from shu.maa_agents import MaaRoleAgents
        from shu.maa_facts import RosterStore
        self.output, self.timeout = Path(output).resolve(), timeout
        self.project_root = Path(project_root).resolve()
        self.resource_root = Path(resource_root).resolve()
        self.source_data = Path(source_data).resolve()
        self.key_path = Path(key_path).resolve()
        self.profile_path = self.project_root / 'workspace-profile.json'
        if self.output.is_relative_to(self.source_data):
            raise ValueError('Acceptance output must be outside the shared source data directory')
        if self.output.exists():
            raise ValueError('Acceptance requires a new isolated output directory')
        self.output.mkdir(parents=True)
        self.data = self.output / 'data'
        self.data.mkdir()
        self.profile_before = optional_bytes(self.profile_path)
        self.source_settings_before = (self.source_data / 'settings.json').read_bytes()
        settings = json.loads(self.source_settings_before)
        write_json(self.data / 'settings.json', {k: settings[k] for k in
                   ('model', 'base_url', 'game_model', 'game_thinking') if k in settings})
        write_json(self.data / 'voice-output.json', {'enabled': False})
        self.box, self.box_hash = source_box(self.source_data)
        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        for font in ('msyh.ttc', 'msyhbd.ttc'):
            QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / font))
        self.app.setFont(QFont('Microsoft YaHei UI', 10))
        self.app.setApplicationVersion(version)
        self.controller = SessionController(self.resource_root, self.data, key_path=self.key_path)
        RosterStore(self.controller.execution.store).publish('local-default', self.box['operators'],
            self.box['complete'], self.box['source'],
            explicit_ownership=self.box.get('explicit_ownership', False))
        self.roles = []
        self.messages, self.events, self.errors, self.routes, self.notices, self.results = [], [], [], [], [], []
        self.MaaRoleAgents = MaaRoleAgents
        self.SessionController, self.CompanionShell = SessionController, CompanionShell
        self.shell = None
        self.report = {'scope': 'real SessionController and DeepSeek roles/tools; isolated SQLite; '
                       'read-only public research; zero MAA/game input',
                       'output': str(self.output), 'source_data': str(self.source_data),
                       'project_root': str(self.project_root), 'resource_root': str(self.resource_root),
                       'version': version, 'frozen': bool(getattr(sys, 'frozen', False)),
                       'key_path_referenced': str(self.key_path), 'key_copied': False,
                       'source_history_copied': False, 'box': {
                           'source_payload_sha256': self.box_hash, 'operator_count': len(self.box['operators']),
                           'complete': self.box['complete'], 'explicit_ownership': self.box.get('explicit_ownership'),
                           'source': self.box['source']},
                       'source_sha256': source_hashes(self.project_root),
                       'source_proof': ('unavailable_in_frozen_runtime' if getattr(sys, 'frozen', False)
                                        else 'source_files_on_disk'),
                       'turns': [], 'checks': {}, 'component_cases': []}
        self.connect()

    def connect(self):
        def factory(*args, **kwargs):
            role = self.MaaRoleAgents(*args, **kwargs)
            self.roles.append(role)
            return role
        c = self.controller
        c.execution.role_factory = factory
        c.message.connect(lambda role, text: self.messages.append({'role': role, 'text': text}))
        c.error.connect(self.errors.append)
        c.notice.connect(self.notices.append)
        c.message_routed.connect(self.routes.append)
        c.execution.finished.connect(self.results.append)
        c.activity_progress.connect(self.event)
        self.shell = self.CompanionShell(c, self.resource_root)

    def event(self, event):
        self.events.append(event)
        with (self.output / 'progress.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')

    def wait(self, seconds=None):
        start = time.monotonic()
        deadline = seconds or self.timeout
        last = start
        while time.monotonic() - start < deadline:
            self.app.processEvents()
            time.sleep(.02)
            pending = self.controller.execution.store.management.messages(pending_only=True)
            if not self.controller.is_busy and not self.controller.execution.active and not pending:
                if time.monotonic() - start > .4:
                    return False
            if time.monotonic() - last > 25:
                last = time.monotonic()
                tasks = self.controller.execution.store.management.list_tasks()
                print(json.dumps({'heartbeat': round(last - start), 'busy': self.controller.is_busy,
                    'active': self.controller.execution.active, 'tasks': [
                        {'task_id': t['task_id'], 'status': t['status']} for t in tasks]}, ensure_ascii=False), flush=True)
        return True

    def state(self, task_id):
        c = self.controller
        state = c.execution.store.current(task_id)
        with c.engine._lock:
            db = c.engine._db
            dispatches = [dict(r) for r in db.execute('SELECT dispatch_id,task_id,role,status,revision,epoch '
                                                     'FROM workflow_dispatches WHERE task_id=? ORDER BY rowid', (task_id,))]
            questions = [dict(r) for r in db.execute('SELECT * FROM workflow_questions WHERE task_id=? ORDER BY rowid', (task_id,))]
            launches = [dict(r) for r in db.execute('SELECT event_id,type,revision,epoch,acked FROM workflow_outbox '
                "WHERE task_id=? AND type IN ('command.submitted','command.modified','command.continue') ORDER BY rowid", (task_id,))]
            receipts = [dict(r) for r in db.execute('SELECT * FROM workflow_request_receipts WHERE task_id=?', (task_id,))]
        graph = {}
        graph_path = self.data / 'maa-graph.sqlite3'
        if graph_path.exists():
            with sqlite3.connect(graph_path.as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                checkpoint = db.execute('SELECT thread_id,checkpoint_id,type,checkpoint FROM checkpoints '
                    'WHERE thread_id=? ORDER BY checkpoint_id DESC LIMIT 1', (task_id,)).fetchone()
                if checkpoint:
                    from shu.maa_graph import JsonOnlySerializer
                    value = JsonOnlySerializer().loads_typed((checkpoint['type'], checkpoint['checkpoint']))
                    channels = value.get('channel_values', {})
                    graph = {'thread_id': checkpoint['thread_id'], 'checkpoint_id': checkpoint['checkpoint_id'],
                        'state': {key: channels.get(key) for key in ('task_id', 'epoch', 'revision', 'phase',
                            'status', 'goal_complete', 'next_role', 'waiting_kind', 'completed_dispatch_ids',
                            'last_role', 'source_dispatch_id', 'code', 'summary')},
                        'checkpoint_count': db.execute('SELECT COUNT(*) FROM checkpoints WHERE thread_id=?',
                                                       (task_id,)).fetchone()[0]}
        result_receipts = {d['dispatch_id']: c.execution.store.dispatch_result(d['dispatch_id'], current_only=False)
                           for d in dispatches}
        return {'task': state, 'dispatches': dispatches, 'result_receipts': result_receipts,
                'questions': questions, 'graph': graph,
                'launch_events': launches, 'request_receipts': receipts,
                'lifecycle': c.execution.store.management.lifecycle(task_id)}

    def screenshot(self, name):
        try:
            self.shell.open_activity()
            self.app.processEvents()
            self.shell.activity.grab().save(str(self.output / (name + '.png')))
            return str(self.output / (name + '.png'))
        except Exception as exc:
            return {'error': type(exc).__name__ + ': ' + str(exc)}

    def turn(self, name, text, *, mode='auto', task_id=None):
        c = self.controller
        c.set_input_target(task_id, mode=mode)
        starts = [len(collection) for collection in (self.messages, self.events, self.errors,
                                                     self.routes, self.notices, self.results)]
        start = time.monotonic()
        c.last_intent_contract = None
        c.submit(text)
        timeout = self.wait()
        self.app.processEvents()
        if timeout:
            c.stop(silent=True)
        collections = (self.messages, self.events, self.errors, self.routes, self.notices, self.results)
        turn = {'name': name, 'text': text, 'input_mode': mode, 'explicit_task_id': task_id,
                'elapsed_seconds': round(time.monotonic() - start, 3), 'timed_out': timeout,
                'contract': c.last_intent_contract}
        for label, collection, begin in zip(('messages', 'events', 'errors', 'routes', 'notices', 'results'), collections, starts):
            turn[label] = collection[begin:]
        routed_id = turn['routes'][-1].get('task_id') if turn['routes'] else None
        turn['routed_task_id'] = routed_id
        if routed_id:
            turn['evidence'] = self.state(routed_id)
            try:
                from shu.code_delivery import CodeDeliveryStore
                turn['delivery'] = CodeDeliveryStore(c.execution.store, c.execution.directory).summary(routed_id, verify_current=True)
            except (ValueError, KeyError, OSError) as exc:
                turn['delivery_error'] = type(exc).__name__ + ': ' + str(exc)
            if (turn.get('contract') or {}).get('current_deliverable') == 'stage_catalog':
                from shu.planned_agents import verify_planned_completion
                from shu.leader_plan import read_reference
                state = turn['evidence']['task']
                candidates = [d for d in turn['evidence']['dispatches'] if d['role'] == 'catalog'
                    and d['revision'] == state['revision'] and d['epoch'] == state['epoch']]
                result = turn['evidence']['result_receipts'].get(candidates[-1]['dispatch_id']) if candidates else {}
                verification = verify_planned_completion(c.execution.store,
                    {**state, 'role_results': {'catalog': result or {}}}, 'stage_catalog')
                ref = (result or {}).get('structure_ref')
                turn['catalog'] = {'verification': verification,
                    'structure': read_reference(ref, c.execution.directory) if ref else None}
        turn['screenshot'] = self.screenshot(name)
        panel = self.shell.activity
        groups, legacy = panel._history.task_groups(c.execution.store.management.messages())
        turn['taskbox'] = {'selected_task_id': panel.selected_task_id,
            'groups': {owner: [record['request_id'] for record in records] for owner, records in groups.items()},
            'legacy_request_ids': [record['request_id'] for record in legacy],
            'visible_task_ids': [row.get('task_id') or row.get('id') for row in panel._task_rows()],
            'question_text': panel.question_card.text()}
        self.report['turns'].append(turn)
        self.save()
        print(json.dumps({'turn': name, 'seconds': turn['elapsed_seconds'], 'routed_task_id': routed_id,
            'status': turn.get('evidence', {}).get('task', {}).get('status'), 'errors': turn['errors'],
            'verified_codes': len(turn.get('delivery', {}).get('code_refs', []))}, ensure_ascii=False), flush=True)
        return turn

    def save(self):
        write_json(self.output / 'report.json', self.report)

    def seed_wait(self):
        """Clearly marked protocol component setup, never a natural-model claim."""
        c = self.controller
        state = c.execution.store.submit('查询孤星一个普通关卡的作业代码，等待用户指定关卡', {
            'goal': '查询孤星一个普通关卡的作业代码，等待用户指定关卡',
            'relation': 'new', 'current_deliverable': 'copilot_codes', 'activity_name': '孤星',
            'constraints': ['只读查询，不导入MAA，不开始作战'], 'match_roster': True, 'profile_id': 'local-default'})
        c.execution.store.suspend(state['task_id'], expected_epoch=state['epoch'],
            message='你要查询孤星哪个普通关卡的作业代码？', kind='business')
        c._task_id = c.execution.current_task_id = state['task_id']
        self.report['component_cases'].append({'name': 'seeded_business_wait_protocol',
            'seeded': True, 'setup': self.state(state['task_id'])})
        self.save()
        return state['task_id']

    def reopen(self):
        self.shell.activity.hide()
        self.shell.pet.hide()
        self.controller.close()
        self.controller = self.SessionController(self.resource_root, self.data, key_path=self.key_path)
        self.connect()
        self.wait(10)

    def lifecycle_case(self, task_id):
        from shu.maa_facts import RosterStore
        c = self.controller
        before = self.state(task_id)
        c.trash_task(task_id)
        trashed = self.state(task_id)
        self.report['lifecycle'] = {'target_task_id': task_id, 'before': before, 'trashed': trashed}
        if not self.shell.activity._recycle:
            self.shell.activity.recycle_button.click()
        self.shell.activity.select_task(task_id, user=False)
        self.report['lifecycle']['trash_screenshot'] = self.screenshot('recycle-bin')
        before_count = len(trashed['dispatches'])
        self.reopen()
        c = self.controller
        reopened = self.state(task_id)
        c.restore_task(task_id)
        self.wait(5)
        restored = self.state(task_id)
        self.report['lifecycle'].update(reopened=reopened, restored=restored,
            restore_screenshot=self.screenshot('restored-task'))
        checks = self.report['checks']
        checks['trash_restart_restore_never_dispatches'] = (
            len(reopened['dispatches']) == len(restored['dispatches']) == before_count
            and trashed['lifecycle']['state'] == reopened['lifecycle']['state'] == 'trashed'
            and restored['lifecycle']['state'] == 'active' and restored['task']['writes_enabled'] is False)
        # Empty synthetic record only. No real conversation/research task purged.
        box_before = RosterStore(c.execution.store).current('local-default')
        other_tasks = {t['task_id']: (t['status'], t['revision'], t['epoch'])
                       for t in c.execution.store.management.list_tasks()}
        synthetic = c.execution.store.submit('Acceptance-only empty synthetic purge target', {
            'current_deliverable': 'stage_catalog', 'relation': 'new'})
        c.trash_task(synthetic['task_id'])
        purged = c.purge_task(synthetic['task_id'])
        box_after = RosterStore(c.execution.store).current('local-default')
        other_after = {t['task_id']: (t['status'], t['revision'], t['epoch'])
                       for t in c.execution.store.management.list_tasks()}
        self.report['synthetic_purge'] = {'seeded': True, 'task_id': synthetic['task_id'], 'result': purged,
            'lifecycle': c.execution.store.management.lifecycle(synthetic['task_id']),
            'box_unchanged': box_before == box_after, 'other_tasks_unchanged': other_tasks == other_after}
        checks['empty_synthetic_purge_preserves_box_and_tasks'] = (box_before == box_after and
            other_tasks == other_after and self.report['synthetic_purge']['lifecycle']['state'] == 'purged')
        self.save()

    def finalize(self):
        c = self.controller
        checks = self.report['checks']
        with c.engine._lock:
            tables = {r[0] for r in c.engine._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.report['counts'] = {name: (c.engine._db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                                            if name in tables else 0)
                for name in ('workflow_messages', 'workflow_questions', 'workflow_dispatches', 'workflow_results',
                             'workflow_operations', 'workflow_request_receipts', 'code_receipts')}
            self.report['submitted_input_count'] = c.engine._db.execute(
                'SELECT COUNT(*) FROM workflow_operations WHERE submitted_at IS NOT NULL').fetchone()[0]
        self.report['metrics'] = {
            'persona': list(c.client.metrics_history),
            'roles': {name: [metric for role in self.roles if name in role.clients
                             for metric in role.clients[name].metrics_history]
                      for name in ('leader', 'catalog', 'research', 'maa', 'vision', 'diagnosis')}}
        # Persisted model_calls includes persona clients created per turn/reopen.
        with c.runtime._lock:
            rows = c.runtime._db.execute('SELECT role,task_id,metrics FROM model_calls ORDER BY id').fetchall()
            self.report['persisted_metrics'] = [{'role': row['role'], 'task_id': row['task_id'],
                                                'metrics': json.loads(row['metrics'])} for row in rows]
        metrics = self.report['persisted_metrics']
        self.report['model_totals'] = {'requests': len(metrics),
            'tokens': sum(m['metrics'].get('usage', {}).get('total_tokens', 0) for m in metrics),
            'roles': dict(Counter(m['role'] for m in metrics))}
        forbidden = {'capture_screen', 'observe_screen', 'game_input', 'inspect_maa', 'maa_act', 'verify_import'}
        self.report['forbidden_tool_attempts'] = [event for event in self.events
            if event.get('stage') == 'tool_started' and event.get('tool') in forbidden]
        checks['no_gui_or_game_input'] = (self.report['submitted_input_count'] == 0
            and not c.screen_capture_allowed and not c.game_input_allowed and not self.report['forbidden_tool_attempts']
            and not self.report['metrics']['roles']['maa'])
        checks['source_profile_and_settings_unchanged'] = (self.profile_before == optional_bytes(self.profile_path)
            and self.source_settings_before == (self.source_data / 'settings.json').read_bytes())
        _, after_hash = source_box(self.source_data)
        checks['source_published_box_unchanged'] = after_hash == self.box_hash
        checks['no_turn_errors_or_timeouts'] = all(not t['errors'] and not t['timed_out'] for t in self.report['turns'])
        self.report['errors'] = list(self.errors)
        self.report['passed'] = bool(checks) and all(value is True for value in checks.values())
        self.save()
        print(json.dumps({'passed': self.report['passed'], 'checks': checks, 'counts': self.report['counts'],
                          'model_totals': self.report['model_totals']}, ensure_ascii=False), flush=True)
        return 0 if self.report['passed'] else 1

    def run(self):
        first = self.turn('01-natural-query-needs-stage',
            '请帮我找孤星一个普通关卡能按我的Box使用的作业，并给出真实神秘代码。'
            '我还没有选具体关卡，请先询问我具体哪一关，等我回答后继续原任务。只读查询，不导入MAA，不开始作战。')
        task_id = first['routed_task_id']
        question = self.controller.execution.store.management.question(task_id) if task_id else None
        natural_wait = bool(question and question['kind'] == 'business')
        self.report['checks']['natural_query_creates_business_question'] = natural_wait
        if not natural_wait:
            task_id = self.seed_wait()
        before = self.state(task_id)
        chat = self.turn('02-chat-while-waiting', '先聊一句：你好，今天也辛苦了。', mode='chat')
        self.report['checks']['chat_does_not_bind_or_modify_waiting_task'] = (
            chat['routed_task_id'] is None and self.state(task_id)['task']['epoch'] == before['task']['epoch'])
        other = self.turn('03-independent-catalog-while-waiting',
            '新建一个独立任务：只核对孤星CW-2普通关卡的关卡目录资料，不查作业代码，不导入MAA，不开始作战。', mode='new')
        self.report['checks']['new_task_runs_while_first_waits'] = (other['routed_task_id'] is not None
            and other['routed_task_id'] != task_id and bool(other.get('evidence', {}).get('dispatches'))
            and self.controller.execution.store.management.question(task_id) is not None)
        self.report['checks']['independent_catalog_verified_by_receipt'] = (
            other.get('evidence', {}).get('task', {}).get('status') == 'completed'
            and other.get('catalog', {}).get('verification', {}).get('complete') is True
            and (other.get('catalog', {}).get('structure') or {}).get('catalog_complete') is True)
        answer = self.turn('04-answer-auto-route-and-research',
            '回答刚才等待我选关卡的作业查询任务：CW-1，普通。继续按我的Box筛选并给我真实作业神秘代码，只查询，不导入MAA，不开始作战。')
        evidence = answer.get('evidence', {})
        answer_task = evidence.get('task', {})
        delivery = answer.get('delivery', {})
        answered = [q for q in self.state(task_id)['questions'] if q['status'] == 'answered']
        checks = self.report['checks']
        checks['answer_binds_same_task_and_closes_question'] = (answer['routed_task_id'] == task_id and bool(answered)
            and any(route.get('relation') == 'answer' for route in answer['routes']))
        checks['answer_really_launches_same_task'] = (answer['routed_task_id'] == task_id
            and len(evidence.get('dispatches', [])) > len(before['dispatches'])
            and any(item['type'] == 'command.continue' for item in evidence.get('launch_events', [])))
        expected_requests = [t['routes'][-1]['request_id'] for t in (first, answer) if t['routes']]
        checks['taskbox_groups_original_and_answer'] = (natural_wait and len(expected_requests) == 2
            and set(expected_requests).issubset(answer['taskbox']['groups'].get(task_id, [])))
        checks['real_box_verified_code_receipts'] = (answer_task.get('status') == 'completed'
            and delivery.get('complete') is True and not delivery.get('missing')
            and len(delivery.get('code_refs', [])) >= len(delivery.get('items', [])) > 0
            and all(item.get('primary_code') for item in delivery.get('items', [])))
        self.report['continuation'] = {'task_id': task_id, 'naturally_created_wait': natural_wait,
            'before': before, 'after': self.state(task_id)}
        self.save()
        self.lifecycle_case(task_id)
        return self.finalize()

    def run_seeded_component(self):
        task_id = self.seed_wait()
        self.report['scope'] += '; seeded business-question protocol component, explicit task input target'
        before = self.state(task_id)
        answer = self.turn('01-seeded-explicit-answer',
            'CW-1，普通。继续按我的Box筛选作业并给出真实神秘代码，只查询，不导入MAA，不开始作战。',
            mode='task', task_id=task_id)
        after = self.state(task_id)
        delivery = answer.get('delivery', {})
        checks = self.report['checks']
        checks['explicit_answer_preserves_task_and_closes_question'] = (
            answer['routed_task_id'] == task_id and any(q['status'] == 'answered' for q in after['questions'])
            and any(route.get('relation') == 'answer' for route in answer['routes']))
        checks['explicit_answer_really_launches_same_task'] = (
            len(after['dispatches']) > len(before['dispatches'])
            and any(item['type'] == 'command.continue' for item in after['launch_events']))
        checks['real_box_verified_code_receipts'] = (after['task']['status'] == 'completed'
            and delivery.get('complete') is True and not delivery.get('missing')
            and len(delivery.get('code_refs', [])) >= len(delivery.get('items', [])) > 0
            and all(item.get('primary_code') for item in delivery.get('items', [])))
        self.report['continuation'] = {'task_id': task_id, 'naturally_created_wait': False,
            'explicit_input_target': True, 'before': before, 'after': after}
        self.save()
        self.lifecycle_case(task_id)
        return self.finalize()

    def run_seeded_lifecycle(self):
        """Waiting-task recovery is separate from completed-query restoration."""
        task_id = self.seed_wait()
        self.report['scope'] += '; seeded waiting-task lifecycle component; no model answer seeded'
        self.lifecycle_case(task_id)
        restored = self.report['lifecycle']['restored']
        self.report['checks']['restored_waiting_task_is_paused_and_question_withdrawn'] = (
            restored['task']['status'] == 'paused' and restored['task']['control_kind'] == 'pause'
            and not restored['task']['writes_enabled']
            and all(q['status'] == 'withdrawn' for q in restored['questions']))
        return self.finalize()

    def close(self):
        if self.shell:
            self.shell.activity.hide()
            self.shell.pet.hide()
        self.controller.close()


def run_task_dialogue_acceptance(project_root, resource_root, output, key_path, source_data,
                                 *, timeout=600, mode='natural', version):
    """Run the packaged product chain without importing an external source script."""
    runners = {'natural': 'run', 'seeded-component': 'run_seeded_component',
               'seeded-lifecycle': 'run_seeded_lifecycle'}
    if mode not in runners:
        raise ValueError('Unknown task dialogue acceptance mode')
    if timeout <= 0:
        raise ValueError('Acceptance timeout must be positive')
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    acceptance = Acceptance(output, timeout, project_root=project_root, resource_root=resource_root,
                            key_path=key_path, source_data=source_data, version=version)
    try:
        return getattr(acceptance, runners[mode])()
    except Exception as exc:
        acceptance.report['fatal_error'] = {'type': type(exc).__name__, 'message': str(exc)}
        acceptance.save()
        raise
    finally:
        acceptance.close()

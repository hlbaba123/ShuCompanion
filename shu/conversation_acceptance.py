"""Isolated product conversation acceptance with explicit native-effect opt-ins."""
import json
import hashlib
import os
from pathlib import Path
import time
import sys


def _persona_clients(controller):
    """Include the independent intent transport without counting shared clients twice."""
    clients = [controller.client]
    intent = getattr(controller, 'intent_client', controller.client)
    if intent is not controller.client:
        clients.append(intent)
    return clients


def install_start_budget(store, *, allowed, target=None, expected_import=None):
    """Add a test-only ceiling before the product's unchanged ledger admission."""
    from .core import ActionRejected
    prepare, issue, submit = store.prepare_operation, store.issue_operation, store.enter_submission

    def check(action, *, issued=False):
        if not allowed():
            raise ActionRejected('本轮验收禁止启动作战。')
        if target is not None:
            ready = store._battle_reference(action.get('ready_ref'))
            actual = ready.get('target') or {}
            if (actual.get('stage_code'), actual.get('difficulty')) != tuple(target):
                raise ActionRejected('实际导航关卡不在本轮单次验收授权范围内。')
            preceding = expected_import() if expected_import is not None else None
            if not preceding or actual.get('mystery_code') != preceding.get('mystery_code'):
                raise ActionRejected('实际运行作业不属于本会话上一轮已核验的导入。')
        with store.engine._lock:
            count = store.engine._db.execute(
                'SELECT COUNT(*) FROM workflow_operations WHERE starts=1 AND issued_at IS NOT NULL').fetchone()[0]
        if count >= (2 if issued else 1):
            raise ActionRejected('本次连续会话的单次启动额度已使用。')

    def prepare_bounded(*args, **kwargs):
        if kwargs.get('starts', args[5] if len(args) > 5 else False):
            check(kwargs.get('action', args[3] if len(args) > 3 else {}))
        return prepare(*args, **kwargs)

    def issue_bounded(identifier, *args, **kwargs):
        operation = store.get_operation(identifier)
        if operation.get('starts'):
            check(operation['action'])
        return issue(identifier, *args, **kwargs)

    def submit_bounded(identifier, *args, **kwargs):
        operation = store.get_operation(identifier)
        if operation.get('starts'):
            check(operation['action'], issued=True)
        return submit(identifier, *args, **kwargs)

    store.prepare_operation, store.issue_operation, store.enter_submission = prepare_bounded, issue_bounded, submit_bounded


def battle_verification(store, task_id):
    """Read the actual operation, dispatch and idle evidence for this task."""
    from .battle_contract import authorized
    try:
        state = store.current(task_id)
        with store.engine._lock:
            rows = store.engine._db.execute('SELECT operation_id FROM workflow_operations '
                'WHERE task_id=? AND starts=1 AND submitted_at IS NOT NULL', (task_id,)).fetchall()
        if len(rows) != 1 or state['status'] != 'completed' or not authorized(state['contract']):
            return {'verified': False, 'code': 'single_run_unverified'}
        operation = store.get_operation(rows[0][0])
        proof = (operation.get('result') or {}).get('evidence_ref')
        result = store.dispatch_result(operation['dispatch_id']) or {}
        if (operation['status'] != 'verified' or operation['epoch'] != state['epoch']
                or operation['revision'] != state['revision'] or not proof
                or result.get('status') != 'battle_finished'
                or result.get('start_operation_id') != operation['operation_id']
                or result.get('external_idle_confirmed') is not True
                or proof not in result.get('evidence_refs', [])):
            return {'verified': False, 'code': 'run_end_receipt_unverified'}
        for reference in result['evidence_refs']:
            store._battle_reference(reference)
        store._battle_idle_proof(operation, proof)
        return {'verified': True, 'start_operation_id': operation['operation_id'],
                'evidence_refs': result['evidence_refs'], 'victory_verified': False}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return {'verified': False, 'code': 'run_proof_invalid'}


def assess_turn(turn, *, prepare_import_only=False, allow_single_battle=False):
    """Judge the typed request from durable evidence, never from reply wording."""
    reasons = []
    contract = turn.get('contract') or {}
    typed = contract.get('current_deliverable')
    if turn.get('timed_out') or turn.get('errors'):
        reasons.append('本轮超时或出错')
    task = turn.get('task') or {}
    if allow_single_battle and (turn.get('battle_verification') or {}).get('verified') is True:
        return {'passed': not reasons, 'reasons': reasons, 'current_deliverable': typed,
                'completion_scope': 'maa_single_run_finished'}
    if typed == 'chat':
        if not any(m.get('role') == 'assistant' and m.get('text') for m in turn.get('messages', [])):
            reasons.append('没有对话回复')
    elif typed == 'stage_catalog':
        catalog = turn.get('catalog') or {}
        structure = catalog.get('structure') or {}
        if task.get('status') != 'completed' or catalog.get('verified') is not True or structure.get('ok') is not True or structure.get('catalog_complete') is not True or not structure.get('groups') or not structure.get('stages'):
            reasons.append('任务未完成或真实目录结构未核实')
    elif typed == 'copilot_codes':
        delivery = turn.get('delivery') or {}
        items = delivery.get('items') or []
        if (task.get('status') != 'completed' or delivery.get('complete') is not True
                or delivery.get('missing') or not items or len(delivery.get('code_refs') or []) < len(items)
                or any(not item.get('primary_code') for item in items)):
            reasons.append('任务未完成或目标关卡的主选凭据不完整')
    elif typed == 'maa_import':
        if prepare_import_only:
            verified = turn.get('preparation_verification') or {}
            if (task.get('status') != 'needs_input' or verified.get('verified') is not True
                    or not verified.get('items') or verified.get('goal_complete') is not False
                    or verified.get('maa_dispatches') != 0 or verified.get('submitted_operations') != 0):
                reasons.append('当前任务导入准备凭据未核实，或越过了只准备边界')
        else:
            verified = turn.get('import_verification') or {}
            if (task.get('status') != 'completed' or verified.get('verified') is not True
                    or verified.get('battle_start_submitted') is not False or not verified.get('evidence_refs')):
                reasons.append('任务未完成或缺少当前 MAA 加载读回凭据')
    else:
        reasons.append('当前交付类型缺失或不在本次验收范围')
    return {'passed': not reasons, 'reasons': reasons, 'current_deliverable': typed}


def import_verification(store, task_id, *, dispatch_id=None):
    """Verify an import from its actual input ledger and visual artifacts."""
    with store.engine._lock:
        state = store.current(task_id)
        query = ("SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r ON r.dispatch_id=d.dispatch_id "
                 "WHERE d.task_id=? AND d.revision=? AND d.epoch=? AND d.role='maa'")
        args = (task_id, state['revision'], state['epoch'])
        if dispatch_id is not None:
            query += " AND d.dispatch_id=?"
            args += (dispatch_id,)
        row = store.engine._db.execute(query + " ORDER BY d.rowid DESC LIMIT 1", args).fetchone()
        receipt = (store.dispatch_result(row['dispatch_id']) or {}) if row else {}
        actions_query = ("SELECT operation_id,dispatch_id,epoch,action,starts,status FROM workflow_operations "
                         "WHERE task_id=? AND submitted_at IS NOT NULL")
        if dispatch_id is not None:
            actions_query += " AND dispatch_id=?"
        actions_args = (task_id,) if dispatch_id is None else (task_id, dispatch_id)
        actions = [dict(item) for item in store.engine._db.execute(actions_query + " ORDER BY rowid", actions_args)]
    task_completed = state['status'] == 'completed' and state['control_kind'] == 'import_verified'
    for item in actions:
        action = json.loads(item.pop('action'))
        item['kind'] = action.get('kind')
        item['control_action'] = action.get('action')
        item['mystery_code'] = action.get('code')
    if receipt.get('queue_receipt_ref'):
        from .maa_queue_completion import verify_queue_result
        with store.engine._lock:
            research_row = store.engine._db.execute(
                "SELECT dispatch_id FROM workflow_dispatches WHERE task_id=? AND revision=? AND epoch=? "
                "AND role='research' ORDER BY rowid DESC LIMIT 1",
                (task_id,state['revision'],state['epoch'])).fetchone()
            research = store.dispatch_result(research_row['dispatch_id']) if research_row else {}
        decision = verify_queue_result(store,state,state['contract'],research or {},receipt)
        valid = (dispatch_id is None and task_completed
                 and decision['goal_complete'] is True and not any(item['starts'] for item in actions))
        return {'verified':valid,'status':receipt.get('status'),'schema':'maa_queue_receipt/v1',
                'coverage':decision,'queue_receipt_ref':receipt['queue_receipt_ref'],
                'evidence_refs':receipt.get('evidence_refs',[]),'battle_start_submitted':False,
                'actions':actions}
    proof = {}
    readback_verified = False
    try:
        from .maa_visual_readback import verify_import
        evidence_root = Path(store.engine.data_dir).resolve()

        def checked_artifact(ref):
            path = Path(ref['path']).resolve(strict=True)
            if not path.is_relative_to(evidence_root):
                raise ValueError('Evidence outside this acceptance run')
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('Oversized acceptance receipt')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref['sha256']:
                raise ValueError('Acceptance receipt changed')
            return json.loads(raw)

        ref = receipt['receipt_ref']
        if ref in receipt.get('evidence_refs', []):
            proof = checked_artifact(ref)
            needed = [proof['before'], *proof['after']]
            code_entry_id = proof.get('code_entry')
            if code_entry_id is not None:
                needed.append(code_entry_id)
            frames = {}
            for observation_ref in proof['observation_refs']:
                if observation_ref not in receipt['evidence_refs']:
                    continue
                frame = checked_artifact(observation_ref)
                identifier = frame.get('screenshot_id')
                if identifier in needed:
                    image = Path(frame['image_path']).resolve(strict=True)
                    if (not image.is_relative_to(evidence_root)
                            or image.stat().st_size > 32 * 1024 * 1024
                            or hashlib.sha256(image.read_bytes()).hexdigest() != frame['image_sha256']):
                        raise ValueError('Acceptance screenshot changed')
                    frames[identifier] = frame
            before = frames[proof['before']]
            after = [frames[identifier] for identifier in proof['after']]
            code_entry = frames[code_entry_id] if code_entry_id is not None else None
            load_receipt = None
            if proof.get('load_receipt_ref') is not None:
                if (proof['load_receipt_ref'] not in receipt['evidence_refs']
                        or proof['load_receipt_ref'] not in proof['observation_refs']):
                    raise ValueError('Application receipt missing from evidence')
                load_receipt = checked_artifact(proof['load_receipt_ref'])
                baseline = load_receipt['baseline']
                baseline_ref = baseline['evidence_ref']
                if (baseline_ref not in receipt['evidence_refs'] or baseline_ref not in proof['observation_refs']
                        or checked_artifact(baseline_ref) != {key: value for key, value in baseline.items()
                                                              if key != 'evidence_ref'}):
                    raise ValueError('Application baseline missing or changed')
            readback_verified = (before.get('field_empty') is True
                                 and not before.get('expected_title_visible')
                                 and verify_import(before, after, expected=proof['expected'],
                                                   code_entry=code_entry, load_receipt=load_receipt)['verified'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    actual_import = any(row and item['dispatch_id'] == row['dispatch_id']
                        and item['epoch'] == state['epoch'] and item['kind'] == 'maa_import'
                        and item['control_action'] == 'replace_code' and item['status'] == 'verified'
                        and item['mystery_code'] == proof.get('code') == receipt.get('mystery_code')
                        and any(action.get('operation_id') == item['operation_id']
                                and action.get('action') == 'replace_code' for action in proof.get('actions', []))
                        for item in actions)
    return {'verified': ((dispatch_id is not None or task_completed) and not any(item['starts'] for item in actions)
                         and receipt.get('status') == 'import_verified'
                         and receipt.get('transport') == 'screenshot_keyboard'
                         and proof.get('transport') == 'screenshot_ocr'
                         and proof.get('start_clicked') is False and readback_verified and actual_import),
            'battle_start_submitted': (True if any(item['starts'] for item in actions)
                                       else receipt.get('battle_start_submitted')),
            'evidence_refs': receipt.get('evidence_refs', []), 'submitted_operations': actions,
            'transport': receipt.get('transport'), 'mystery_code': receipt.get('mystery_code')}


def run_conversation_acceptance(project_root,resource_root,output,questions_path,key_path,data_source, version='0.3.9-retry', *, allow_maa_import=False, timeout_seconds=420, prepare_import_only=False, permission_handoff=False, allow_maa_queue_import=False, allow_single_battle=False, single_battle_target=None):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont,QFontDatabase
    from .controller import SessionController
    from .companion_ui import CompanionShell
    from .maa_agents import MaaRoleAgents
    from .core import TaskEngine
    from .workflow_store import WorkflowStore
    from .maa_facts import RosterStore
    from .planned_agents import verify_planned_completion
    from .leader_plan import read_reference
    from .code_delivery import CodeDeliveryStore
    if type(timeout_seconds) not in (int,float) or not 1 <= timeout_seconds <= 3600:
        raise ValueError('Acceptance timeout must be between 1 and 3600 seconds')
    if prepare_import_only and allow_maa_import:
        raise ValueError('Preparation acceptance cannot grant GUI input')
    if permission_handoff and not allow_maa_import:
        raise ValueError('Permission handoff test requires explicit import opt-in')
    if allow_maa_queue_import and not allow_maa_import:
        raise ValueError('Queue acceptance requires explicit import opt-in')
    if allow_single_battle and (not allow_maa_import or allow_maa_queue_import or prepare_import_only or permission_handoff):
        raise ValueError('Single-run conversation requires independent explicit single-import opt-in')
    if bool(single_battle_target) != bool(allow_single_battle):
        raise ValueError('Single-run acceptance requires a separate authorized target bound only at the input gate')
    output=Path(output).resolve();project_root=Path(project_root).resolve()
    if output.exists():raise ValueError('Acceptance requires a new isolated output directory')
    questions=json.loads(Path(questions_path).read_text(encoding='utf-8-sig'))
    if not isinstance(questions,list) or not 1<=len(questions)<=8 or any(not isinstance(q,str) or not q.strip() for q in questions):
        raise ValueError('Questions must be a list of 1 to 8 real user messages')
    output.mkdir(parents=True);data=output/'data';data.mkdir()
    settings=json.loads((Path(data_source)/'settings.json').read_text(encoding='utf-8'))
    (data/'settings.json').write_text(json.dumps({k:settings[k] for k in ('model','base_url','game_model','game_thinking','intent_thinking','leader_thinking') if k in settings}),encoding='utf-8')
    (data/'voice-output.json').write_text('{"enabled":false}',encoding='utf-8')
    appearance=Path(data_source)/'pet-appearance.json'
    if appearance.is_file():
        skin=json.loads(appearance.read_text(encoding='utf-8'))
        if isinstance(skin.get('skin_id'),str):
            (data/'pet-appearance.json').write_text(json.dumps({'skin_id':skin['skin_id']}),encoding='utf-8')
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    app.setApplicationVersion(version)
    for font in ('msyh.ttc','msyhbd.ttc'):QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/font))
    app.setFont(QFont('Microsoft YaHei UI',10))
    controller=SessionController(Path(resource_root),data,key_path=Path(key_path))
    current_turn = {'index': -1}
    preceding_import = {}

    def current_preceding_import():
        if not preceding_import:
            return None
        fresh = import_verification(controller.execution.store, preceding_import['task_id'])
        if (fresh.get('verified') is True
                and fresh.get('mystery_code') == preceding_import['mystery_code']
                and fresh.get('evidence_refs') == preceding_import['evidence_refs']):
            return preceding_import
        return None

    install_start_budget(controller.execution.store,
        allowed=lambda: allow_single_battle and current_turn['index'] == len(questions) - 1,
        target=single_battle_target, expected_import=current_preceding_import)
    controller.execution.queue_import_enabled = bool(allow_maa_queue_import)
    if prepare_import_only:
        controller.execution.single_import_enabled = False
    if allow_maa_import and not permission_handoff:
        controller.screen_capture_allowed = True
        controller.game_input_allowed = True
    # Carry already-redacted recent conversations into the isolated acceptance
    # profile. An empty history cannot represent the reported chat token cost.
    from .memory import ConversationMemory
    seeded_history_count = 0
    if not allow_single_battle:
        history=ConversationMemory(Path(data_source))
        try:
            prior_messages = history.model_messages(last_turns=4)
        finally: history.close()
        for message in prior_messages:
            if message.get('role') in ('user','assistant') and isinstance(message.get('content'),str):
                controller.memory.append(message['role'],message['content'])
                seeded_history_count += 1
    # Copy actual published facts, including explicit row absence. No invented
    # roster or public requirement is used as account information.
    source_engine=TaskEngine(Path(data_source))
    try:box=RosterStore(WorkflowStore(source_engine)).current('local-default')
    finally:source_engine.close()
    if box:RosterStore(controller.execution.store).publish('local-default',box['operators'],box['complete'],box['source'],explicit_ownership=box.get('explicit_ownership',False))
    shell=CompanionShell(controller,Path(resource_root));shell.show_pet()
    roles=[];messages=[];events=[];results=[];errors=[]
    def factory(*args,**kwargs):
        role=MaaRoleAgents(*args,**kwargs);roles.append(role);return role
    controller.execution.role_factory=factory
    controller.message.connect(lambda role,text:messages.append({'role':role,'text':text}))
    def record_progress(event):
        events.append(event)
        with (output/'progress.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event,ensure_ascii=False)+'\n')
    controller.activity_progress.connect(record_progress);controller.execution.finished.connect(results.append);controller.error.connect(errors.append)
    report={'scope':('continuous real pet conversation + verified import + at most one MAA start on final turn' if allow_single_battle else
                    'real pet conversation + verified import preparation; no GUI input' if prepare_import_only else
                    'real pet conversation + MAA import only' if allow_maa_import else
                     'real pet conversation + real model/roles/tools; no MAA/game input'),
        'runtime':{'frozen':bool(getattr(sys,'frozen',False)),'executable':sys.executable,
                   'version':app.applicationVersion(),'resource_root':str(resource_root),
                   'skin_id':shell.pet.canvas.skin_id,'seeded_history_count':seeded_history_count,
                   'single_battle_target':single_battle_target},'turns':[]}
    report['runtime']['thinking'] = {
        name: {'enabled': config.thinking, 'max_tokens': config.max_tokens,
               'timeout_seconds': config.timeout_seconds}
        for name, config in (('intent', getattr(controller, 'intent_config', controller.config)),
                             ('leader', getattr(controller, 'leader_config', controller.config)))
    }
    def metrics_totals():
        metrics=[m for client in _persona_clients(controller) for m in client.metrics_history]+[m for obj in roles for client in obj.clients.values() for m in client.metrics_history]
        return {'model_requests':len(metrics),'total_tokens':sum(m.get('usage',{}).get('total_tokens',0) for m in metrics)}
    try:
        handoff = None
        for index,question in enumerate(questions):
            current_turn['index'] = index
            usage_before=metrics_totals()
            starts=(len(messages),len(events),len(errors),len(results));started=time.monotonic();controller.last_intent_contract=None
            shell.pet.text_submitted.emit(question)
            while time.monotonic()-started<timeout_seconds:
                app.processEvents();time.sleep(.02)
                if (not controller.is_busy and not controller.execution.active
                        and not controller.dialogue.pending and time.monotonic()-started>.2):
                    if permission_handoff and handoff is None:
                        task_id=controller._task_id
                        state=controller.execution.store.current(task_id) if task_id else {}
                        # Render the genuine panel without activating another
                        # desktop surface during the import-only acceptance.
                        from PySide6.QtCore import Qt
                        shell.activity.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating,True)
                        shell.refresh();shell.activity.show();shell.activity.select_task(task_id);app.processEvents()
                        with controller.engine._lock:
                            submitted=controller.engine._db.execute('SELECT COUNT(*) FROM workflow_operations WHERE submitted_at IS NOT NULL').fetchone()[0]
                        button=shell.activity.maa_permission_button
                        handoff={'task_id_before':task_id,'epoch_before':state.get('epoch'),
                            'initial_screen_permission':controller.screen_capture_allowed,
                            'initial_input_permission':controller.game_input_allowed,
                            'blocked_status':state.get('status'),'inputs_before_click':submitted,
                            'button_visible':button.isVisible(),'button_enabled':button.isEnabled()}
                        shell.activity.grab().save(str(output/'permission-before-click.png'))
                        if state.get('status')!='needs_input' or submitted!=0 or not button.isVisible() or not button.isEnabled():
                            raise ValueError('Expected real permission-blocked task and zero native input')
                        button.click();app.processEvents()
                        handoff.update(task_id_after=controller._task_id,
                            epoch_after=controller.execution.store.current(task_id)['epoch'],
                            input_enabled_after=controller.game_input_allowed)
                        shell.activity.grab().save(str(output/'permission-after-click.png'))
                        continue
                    break
            timeout=bool(controller.is_busy or controller.execution.active or controller.dialogue.pending)
            if timeout:controller.stop(silent=True)
            app.processEvents();shell.open_activity();app.processEvents()
            shell.activity.grab().save(str(output/f'turn-{index+1}.png'))
            turn={'question':question,'elapsed_seconds':round(time.monotonic()-started,3),'timed_out':timeout,
                'contract':controller.last_intent_contract,'task':controller.current_task(),
                'messages':messages[starts[0]:],'events':events[starts[1]:],
                'errors':errors[starts[2]:],'results':results[starts[3]:],
                'delivery':controller.execution.research_output()}
            typed=(turn['contract'] or {}).get('current_deliverable')
            task_id=(turn['task'] or {}).get('id') or (turn['task'] or {}).get('task_id')
            if allow_single_battle and task_id:
                turn['battle_verification'] = battle_verification(controller.execution.store, task_id)
                turn['durable_contract'] = controller.execution.store.current(task_id)['contract']
            if task_id and typed == 'maa_import':
                if prepare_import_only:
                    from .import_preparation import verify_import_preparation_result
                    turn['preparation_verification'] = verify_import_preparation_result(
                        controller.execution.store, task_id)
                else:
                    turn['import_verification'] = import_verification(controller.execution.store, task_id)
                    if allow_single_battle and turn['import_verification'].get('verified') is True:
                        preceding_import.clear()
                        preceding_import.update(task_id=task_id,
                            mystery_code=turn['import_verification']['mystery_code'],
                            evidence_refs=turn['import_verification']['evidence_refs'])
            if task_id and typed in ('stage_catalog','copilot_codes'):
                try:
                    state=controller.execution.store.current(task_id)
                    if typed == 'stage_catalog':
                        store=controller.execution.store
                        with controller.execution.engine._lock:
                            row=controller.execution.engine._db.execute(
                                "SELECT d.dispatch_id FROM workflow_dispatches d JOIN workflow_results r ON r.dispatch_id=d.dispatch_id "
                                "WHERE d.task_id=? AND d.revision=? AND d.epoch=? AND d.role='catalog' ORDER BY d.rowid DESC LIMIT 1",
                                (task_id,state['revision'],state['epoch'])).fetchone()
                            result=store.dispatch_result(row['dispatch_id'],current_only=False) if row else None
                        state['role_results']={'catalog':result or {}}
                        verification=verify_planned_completion(controller.execution.store,state,typed)
                        ref=(state.get('role_results',{}).get('catalog') or {}).get('structure_ref')
                        structure=read_reference(ref,controller.execution.engine.data_dir)
                        turn['catalog']={'verified':verification.get('complete') is True,
                                         'verification':verification,'structure':structure}
                    else:
                        turn['delivery']=CodeDeliveryStore(controller.execution.store,controller.execution.directory).summary(task_id,verify_current=True)
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    turn['evidence_error']=type(exc).__name__
            turn['assessment']=assess_turn(turn,prepare_import_only=prepare_import_only,allow_single_battle=allow_single_battle)
            usage_after=metrics_totals()
            turn['usage']={key:usage_after[key]-usage_before[key] for key in usage_before}
            report['turns'].append(turn)
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'turn':index+1,'elapsed':turn['elapsed_seconds'],'errors':turn['errors'],
                'task_status':turn['task'].get('status') if turn['task'] else None,
                'delivered':sum(bool(item.get('primary_code') or item.get('mystery_code'))
                    for item in turn['delivery'].get('items',[]))},ensure_ascii=False),flush=True)
            if timeout:break
        report['role_metrics']={r:[m for obj in roles if r in obj.clients for m in obj.clients[r].metrics_history] for r in ('leader','catalog','research','maa','vision','diagnosis')}
        report['catalog_metrics']=report['role_metrics']['catalog']
        report['intent_metrics']=list(getattr(controller,'intent_client',controller.client).metrics_history)
        report['persona_metrics']=list(controller.client.metrics_history)
        report['screen_permission']=controller.screen_capture_allowed;report['input_permission']=controller.game_input_allowed
        forbidden={'capture_screen','observe_screen','game_input'}
        if not allow_maa_import:forbidden.update({'inspect_maa','maa_act','verify_import'})
        report['forbidden_tool_attempts']=[{'stage':e.get('stage'),'tool':e.get('tool')} for e in events
                                           if e.get('stage') == 'tool_started' and e.get('tool') in forbidden]
        report['completed_without_errors']=len(report['turns'])==len(questions) and all(not t['errors'] and not t['timed_out'] for t in report['turns'])
        report['all_turns_verified']=all(t['assessment']['passed'] for t in report['turns'])
        permission_scope_ok = allow_maa_import or (not report['screen_permission'] and not report['input_permission'] and not report['role_metrics']['maa'])
        if prepare_import_only:
            report['verified_preparation_count'] = sum(
                t['assessment']['passed'] and t['assessment']['current_deliverable'] == 'maa_import'
                for t in report['turns'])
            permission_scope_ok = permission_scope_ok and report['verified_preparation_count'] > 0
        if allow_maa_import:
            # A valid chat response is insufficient for an import acceptance run.
            report['verified_import_count'] = sum(
                t['assessment']['passed'] and t['assessment']['current_deliverable'] == 'maa_import'
                for t in report['turns'])
            with controller.execution.engine._lock:
                report['battle_start_submissions'] = controller.execution.engine._db.execute(
                    'SELECT COUNT(*) FROM workflow_operations WHERE starts=1 AND submitted_at IS NOT NULL'
                ).fetchone()[0]
            if allow_single_battle:
                report['preceding_import_binding'] = dict(preceding_import)
                permission_scope_ok = (report['verified_import_count'] > 0
                    and report['battle_start_submissions'] == 1
                    and report['turns'][-1].get('battle_verification', {}).get('verified') is True)
            else:
                permission_scope_ok = report['verified_import_count'] > 0 and report['battle_start_submissions'] == 0
        report['passed']=bool(report['completed_without_errors'] and report['all_turns_verified']
            and permission_scope_ok and not report['forbidden_tool_attempts'])
        if permission_handoff:
            report['permission_handoff']=handoff
            report['passed']=bool(report['passed'] and handoff
                and handoff['task_id_before']==handoff['task_id_after']
                and handoff['epoch_after']>handoff['epoch_before']
                and handoff['initial_input_permission'] is False and handoff['input_enabled_after'] is True)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return 0 if report['passed'] else 1
    finally:shell.quit_application();controller.close()

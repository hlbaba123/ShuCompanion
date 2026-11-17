"""Isolated acceptance probes, with explicit input opt-in for execution tests."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import sys
import time


def run_execution_probe(app_root: Path, resource_root: Path, output: Path,
                        goal: str, key_path: Path, click_limit: int = 6,
                        start_delay_ms: int = 200) -> int:
    """Explicitly authorized navigation test; at most six clicks, no drags.

    Called only with both --execution-probe and --allow-game-input. Uses the
    production persona/tools/executor and an isolated history, never user tasks.
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from .controller import SessionController
    from .companion_ui import CompanionShell
    from .windows import WindowError
    from .permissions import inspect_game_input_permission

    if type(click_limit) is not int or not 1<=click_limit<=6:raise ValueError('click limit must be 1..6')
    if type(start_delay_ms) is not int or not 0<=start_delay_ms<=30000:raise ValueError('invalid preparation delay')
    output.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    controller=SessionController(app_root,output/'data',key_path=key_path)
    controller.execution_max_steps=6
    controller.execution_max_seconds=240
    controller.execution.segment_steps=6
    controller.execution.max_decisions=24
    controller.execution.max_seconds=240
    report={'status':'failed','mode':'explicitly_authorized_navigation','max_clicks':click_limit,
            'clicks':[],'click_attempts':0,'messages':[],'errors':[],'isolated_profile':True,
            'own_elevated':inspect_game_input_permission({}).get('own_elevated')}
    window=CompanionShell(controller,resource_root)
    window.show_pet()
    click=controller.windows.controlled_click
    def bounded_click(*args,**kwargs):
        if report['click_attempts']>=click_limit:raise WindowError('本次测试已达到授权点击上限。','test_limit')
        report['click_attempts']+=1
        result=click(*args,**kwargs)
        report['clicks'].append(result)
        return result
    def forbid_drag(*args,**kwargs):
        raise WindowError('本次测试仅授权界面点击，没有授权拖动。','test_scope')
    controller.windows.controlled_click=bounded_click
    controller.windows.controlled_drag=forbid_drag
    controller.message.connect(lambda role,text:report['messages'].append({'role':role,'text':text}))
    controller.error.connect(report['errors'].append)
    controller.notice.connect(lambda message:print(json.dumps({'notice':message},ensure_ascii=True),flush=True) if sys.stdout else None)
    started=time.monotonic();finished=False
    def finish():
        nonlocal finished
        if finished:return
        finished=True
        task=controller.current_task()
        report['task']=task
        report['events']=controller.engine.events(task['id']) if task else []
        report['status']='passed' if task and task['status']=='completed' and not report['errors'] else 'needs_input'
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        (output/'execution-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        window.quit_application()
    def poll():
        if not controller.is_busy and not controller.execution.active:finish()
    timer=QTimer();timer.setInterval(200);timer.timeout.connect(poll)
    def begin():
        controller.set_capture_permission(True)
        controller.set_game_input_permission(True)
        controller.submit(goal)
        timer.start()
    if start_delay_ms>=1000:
        window.pet.present_message('system',f'导航验收将在 {start_delay_ms//1000} 秒后开始，请切到游戏主页并等待。最多 {click_limit} 次导航点击，不消耗资源。')
    QTimer.singleShot(start_delay_ms,begin)
    QTimer.singleShot(330000,lambda:(controller.stop(),finish()))
    try:app.exec()
    finally:controller.close()
    if sys.stdout:print(json.dumps({'status':report['status'],'clicks':len(report['clicks']),
        'errors':report['errors'],'report':str(output/'execution-report.json')},ensure_ascii=True),flush=True)
    return 0 if report['status']=='passed' else 1


def run_ui_capture_probe(app_root: Path, resource_root: Path, output: Path, hwnd: int) -> int:
    """Exercise the real Qt capture entry using only an isolated local profile."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from .controller import SessionController
    from .companion_ui import CompanionShell

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile_root = output / 'isolated-root'
    profile_root.mkdir(exist_ok=True)
    empty_key = profile_root / 'empty-key.txt'
    empty_key.write_text('# Read-only UI diagnostic: no API credential.\n', encoding='utf-8')
    # ProviderConfig can fall back to this environment variable. Remove it only
    # inside this diagnostic and restore it on return; never print its value.
    previous_key = os.environ.pop('DEEPSEEK_API_KEY', None)
    report = {
        'status': 'failed', 'mode': 'pet_ui_strict_background',
        'isolated_profile': True, 'user_key_read': False,
        'game_inputs': 0, 'model_requests': 0, 'activated_target': False,
        'errors': [], 'checks': {},
    }
    started = time.monotonic()
    controller = window = None
    finished = False
    previous_hook = sys.excepthook
    app = QApplication.instance() or QApplication(sys.argv[:1])
    if not isinstance(app, QApplication):
        report['errors'].append('A non-GUI Qt application already owns this process')
        (output / 'ui-capture-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        if previous_key is not None:
            os.environ['DEEPSEEK_API_KEY'] = previous_key
        return 1
    app.setApplicationName('ShuCaptureDiagnostic')
    app.setFont(QFont('Microsoft YaHei UI', 10))
    app.setQuitOnLastWindowClosed(False)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetForegroundWindow.argtypes = []
    user.GetForegroundWindow.restype = ctypes.c_void_p

    def foreground():
        return int(user.GetForegroundWindow() or 0)

    def write_report():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (output / 'ui-capture-report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def finish():
        nonlocal finished
        if finished:
            return
        finished = True
        report.setdefault('foreground_after', foreground())
        if 'foreground_before' in report:
            report['foreground_unchanged'] = report['foreground_before'] == report['foreground_after']
        write_report()
        # UI teardown precedes database closure; quit_application can call stop.
        if window is not None:
            window.quit_application()
        else:
            app.quit()

    def on_error(message):
        report['errors'].append(str(message)[:700])
        if not finished:
            QTimer.singleShot(0, finish)

    def exception_hook(kind, value, traceback):
        # Keep arbitrary exception text (which could contain secrets) out of logs.
        report['errors'].append('Qt callback exception: ' + kind.__name__)
        QTimer.singleShot(0, finish)

    def deadline():
        if not finished:
            report['errors'].append('UI capture did not finish within 15 seconds')
            finish()

    try:
        report['foreground_initial'] = foreground()
        controller = SessionController(profile_root, output / 'data', key_path=empty_key)

        def forbid_model(*args, **kwargs):
            report['model_requests'] += 1
            raise RuntimeError('Model requests are forbidden in this diagnostic')

        def forbid_input(*args, **kwargs):
            report['game_inputs'] += 1
            raise RuntimeError('Game inputs are forbidden in this diagnostic')

        controller.client.complete = forbid_model
        controller.game_client.complete = forbid_model
        controller.windows.controlled_click = forbid_input
        controller.windows.set_input_enabled(False)
        controller.error.connect(on_error)
        window = CompanionShell(controller, Path(resource_root))
        window.pet.setWindowTitle('黍 · 只读后台捕获诊断')
        sys.excepthook = exception_hook
        controller.set_capture_permission(True)
        target = controller.bind_window(int(hwnd))
        report['bound_window'] = target.to_dict()
        report['game_foreground_before'] = target.foreground
        controller.submit('仅诊断：停止后的新只读观察应仍能显示，不执行游戏', 2)
        controller.stop(silent=True)
        stopped = controller.current_task()
        report['stopped_task_status'] = stopped['status'] if stopped else None
        window.show_pet()
        report['activity_initially_visible'] = window.activity.isVisible()

        def on_observation(observation):
            if finished:
                return
            # CompanionShell connected its observation slot during construction,
            # before this callback: these checks cover the actual UI update.
            current = controller.windows.bound_window()
            displayed = window._displayed_observation
            report['observation'] = observation
            report['foreground_after'] = foreground()
            report['game_foreground_after'] = current.foreground if current else None
            report['checks'] = {
                'bound_window_matches': current is not None and current.hwnd == int(hwnd),
                'preview_pixmap_loaded': not window.preview.original.isNull(),
                'displayed_observation_matches': displayed is not None and displayed.get('id') == observation.get('id'),
                'method_is_wgc': observation.get('method') == 'windows_graphics_capture',
                'background_capture': observation.get('background_capture') is True,
                'foreground_unchanged': report['foreground_after'] == report.get('foreground_before'),
                'game_remained_background': target.foreground is False and current is not None and current.foreground is False,
                'host_stayed_hidden': not window.isVisible(),
                'stopped_task_was_cancelled': report['stopped_task_status'] == 'cancelled',
                'no_model_requests': report['model_requests'] == 0,
                'no_game_inputs': report['game_inputs'] == 0,
            }
            # QWidget.grab renders this application's own widget. It does not
            # show/activate the hidden host or capture a desktop crop.
            preview_path = output / 'activity-preview.png'
            report['activity_preview_saved'] = window.activity.grab().save(str(preview_path))
            report['activity_preview_path'] = str(preview_path)
            report['checks']['activity_preview_saved'] = report['activity_preview_saved']
            report['status'] = 'passed' if all(report['checks'].values()) and not report['errors'] else 'failed'
            QTimer.singleShot(0, finish)

        controller.observation_ready.connect(on_observation)

        def begin_capture():
            if finished:
                return
            report['foreground_before'] = foreground()
            window.capture_from_pet()

        QTimer.singleShot(200, begin_capture)
        QTimer.singleShot(15000, deadline)
        app.exec()
        if not finished:
            report['errors'].append('Qt event loop exited before observation completed')
            finish()
    except Exception as exc:
        report['errors'].append('Diagnostic exception: ' + type(exc).__name__)
        finish()
    finally:
        sys.excepthook = previous_hook
        if controller is not None:
            controller.close()
        if previous_key is not None:
            os.environ['DEEPSEEK_API_KEY'] = previous_key
        else:
            os.environ.pop('DEEPSEEK_API_KEY', None)
        write_report()
    if sys.stdout:
        print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status'] == 'passed' else 1

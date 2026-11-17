"""Shu Companion native desktop entry point."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

APP_VERSION = '0.3.47-control'

KEY_TEMPLATE = """# 在下一行填写你的 DeepSeek API Key，然后保存。
# 不要把 Key 发到聊天或提交到代码仓库。
# 可以只写 Key，也可以写 DEEPSEEK_API_KEY=你的Key。

"""


def main() -> int:
    parser = argparse.ArgumentParser(description="黍 · 同行，多智能体桌面宠物")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--key-file", type=Path, help="Use an existing local Key text file")
    parser.add_argument("--self-test", action="store_true", help="离线检查桌宠、进度与设置，不调用模型或游戏输入")
    parser.add_argument('--verify-skin-vignette',metavar='SKIN_ID',help='Offline isolated skin/cat idle-scene acceptance; requires --self-test and a new --data-dir')
    parser.add_argument('--task-overlap-live-test',type=Path,metavar='OUTPUT',
                        help='Explicit backed-up current-profile overlap/recovery test; imports only, never battle')
    parser.add_argument('--maa-battle-test',type=Path,metavar='OUTPUT',help='Explicit real one-loaded-job navigation/start acceptance')
    parser.add_argument('--maa-focus-test',type=Path,metavar='OUTPUT',help='Verify foreground handoff and background MAA readback, without starting a battle')
    parser.add_argument('--battle-import-report',type=Path,help='Bind battle acceptance to the exact preceding verified single-import report')
    parser.add_argument('--prior-focus-profile',type=Path,help='Read-only receipt profile for a specifically proven prior focus-only failure; requires --maa-battle-test')
    parser.add_argument('--prior-focus-state-sha256',help='Explicit immutable legacy worker-state binding for focus-only recovery')
    parser.add_argument("--snapshot", type=Path, help="保存自身窗口截图")
    parser.add_argument("--quit-after-ms", type=int, default=0)
    parser.add_argument("--activity", action="store_true", help="Open the pet's task activity panel")
    parser.add_argument("--workbench", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--capture-probe",type=int,metavar='HWND',help='Read-only background capture diagnostic; no Key or model requests')
    parser.add_argument("--capture-output",type=Path,help='Diagnostic output directory')
    parser.add_argument("--ui-capture-probe",action='store_true',help='Verify the pet/controller/preview path in an isolated read-only profile')
    parser.add_argument('--execution-probe',metavar='GOAL',help='Explicit navigation acceptance using real models and isolated task data')
    parser.add_argument('--allow-game-input',action='store_true',help='Required explicit opt-in for the execution diagnostic (at most six clicks)')
    parser.add_argument('--probe-click-limit',type=int,choices=range(1,7),default=6,help='Lower the explicit navigation diagnostic click limit')
    parser.add_argument('--wait-for-profile',action='store_true',help='Wait briefly for the previous instance to release its profile during a requested restart')
    parser.add_argument('--probe-start-delay-ms',type=int,default=200,help='Preparation delay before an explicitly authorized navigation diagnostic')
    parser.add_argument('--candidates-probe',metavar='STAGE',help='Read-only public station diagnostic; no Key, model, MAA or game input')
    parser.add_argument('--candidates-output',type=Path,help='Isolated directory for public candidate probe evidence')
    parser.add_argument('--conversation-test',type=Path,help='Explicit read-only real-model conversation acceptance; questions JSON')
    parser.add_argument('--allow-maa-queue-conversation-test',action='store_true',help='Explicit multi-document import-only opt-in for continuous conversation acceptance')
    parser.add_argument('--allow-single-battle-conversation-test',action='store_true',help='Explicit one-start budget on the final turn of one continuous conversation')
    parser.add_argument('--single-battle-target',metavar='STAGE:MODE',help='Input-gate-only authorized acceptance target, e.g. TL-EX-1:raid; never sent to the model')
    parser.add_argument('--conversation-output',type=Path,help='New isolated acceptance output directory')
    parser.add_argument('--import-preparation-only',action='store_true',help='Verify the real query-to-preparation handoff without GUI input')
    parser.add_argument('--task-dialogue-test',type=Path,metavar='OUTPUT',
                        help='Read-only isolated real-model task dialogue and task lifecycle acceptance')
    parser.add_argument('--task-dialogue-mode',choices=('natural','seeded-component','seeded-lifecycle'),
                        default='natural',help='Natural dialogue by default; seeded-lifecycle uses no model')
    parser.add_argument('--task-dialogue-timeout',type=int,default=600,metavar='SECONDS',
                        help='Maximum wait for each task dialogue acceptance turn')
    parser.add_argument('--maa-readiness-output',type=Path,help='Read-only MAA screenshot, OCR and input privilege diagnostic')
    parser.add_argument('--maa-queue-probe',type=Path,metavar='OUTPUT',help='Read-only MAA queue screenshot and whitelist diagnostic')
    parser.add_argument('--queue-probe-vision',action='store_true',help='Use configured vision for the read-only queue probe')
    parser.add_argument('--maa-queue-test',type=Path,metavar='OUTPUT',help='Isolated MAA queue acceptance; default read-only probe')
    parser.add_argument('--maa-queue-mode',choices=('probe','component','natural'),default='probe')
    parser.add_argument('--maa-queue-request-file',type=Path,help='JSON containing only the original user request')
    parser.add_argument('--allow-maa-queue-import-test',action='store_true',help='Explicit import-only opt-in for queue acceptance')
    parser.add_argument('--maa-queue-timeout',type=int,default=900,metavar='SECONDS')
    parser.add_argument('--maa-readiness-wait',type=int,choices=range(61),default=0,
                        metavar='SECONDS',help='Wait for the MAA window to be restored')
    parser.add_argument('--allow-maa-import-test',action='store_true',help='Explicit opt-in for isolated conversation acceptance to operate MAA import GUI only')
    parser.add_argument('--maa-permission-handoff-test',action='store_true',help='Test initially disabled permissions and click the actual task authorization button before import')
    args = parser.parse_args()
    import re
    if args.verify_skin_vignette is not None:
        if not args.self_test or args.data_dir is None:
            parser.error('--verify-skin-vignette requires --self-test and an explicit --data-dir')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',args.verify_skin_vignette):
            parser.error('--verify-skin-vignette requires a valid skin ID')
        allowed={'--self-test','--data-dir','--verify-skin-vignette'}
        if any(argument.startswith('--') and argument.split('=',1)[0] not in allowed for argument in sys.argv[1:]):
            parser.error('--verify-skin-vignette accepts only --self-test and --data-dir')
    if (bool(args.single_battle_target) != bool(args.allow_single_battle_conversation_test)
            or args.single_battle_target and not re.fullmatch(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+:(?:normal|raid)',args.single_battle_target)):
        parser.error('--single-battle-target STAGE:normal|raid is required only for single-run conversation acceptance')
    if args.allow_single_battle_conversation_test and (not args.conversation_test or not args.allow_maa_import_test
            or args.allow_maa_queue_conversation_test or args.import_preparation_only or args.maa_permission_handoff_test):
        parser.error('--allow-single-battle-conversation-test requires single-import conversation opt-in')
    if args.maa_focus_test and len(sys.argv) != 3:
        parser.error('--maa-focus-test takes only a new evidence directory')
    if args.battle_import_report and not args.maa_battle_test:
        parser.error('--battle-import-report requires --maa-battle-test')
    if args.maa_battle_test and any((args.self_test,args.task_overlap_live_test,args.capture_probe,
            args.capture_output,args.ui_capture_probe,args.execution_probe,args.candidates_probe,
            args.candidates_output,args.conversation_test,args.conversation_output,args.import_preparation_only,
            args.task_dialogue_test,args.maa_readiness_output,args.maa_queue_probe,args.queue_probe_vision,
            args.maa_queue_test,args.allow_game_input,args.allow_maa_import_test,args.allow_maa_queue_conversation_test,
            args.allow_maa_queue_import_test,args.maa_permission_handoff_test,args.activity,args.workbench,
            args.snapshot,args.quit_after_ms,args.maa_readiness_wait)):
        parser.error('--maa-battle-test cannot be combined with other diagnostics or input opt-ins')
    if bool(args.prior_focus_profile)!=bool(args.prior_focus_state_sha256) or (args.prior_focus_profile and not args.maa_battle_test):
        parser.error('Focus recovery requires --maa-battle-test and both --prior-focus-profile/--prior-focus-state-sha256')
    if args.allow_maa_queue_conversation_test and not (args.conversation_test and args.allow_maa_import_test):
        parser.error('--allow-maa-queue-conversation-test requires --conversation-test and --allow-maa-import-test')
    if args.maa_permission_handoff_test and not (args.conversation_test and args.allow_maa_import_test):
        parser.error('--maa-permission-handoff-test requires --conversation-test and --allow-maa-import-test')
    if args.import_preparation_only and (not args.conversation_test or args.allow_maa_import_test):
        parser.error('--import-preparation-only requires --conversation-test and cannot grant MAA input')
    if (args.maa_queue_request_file or args.allow_maa_queue_import_test
            or args.maa_queue_mode != 'probe') and not args.maa_queue_test:
        parser.error('Queue acceptance options require --maa-queue-test')
    if args.maa_queue_test and (args.self_test or args.conversation_test or args.execution_probe
            or args.task_dialogue_test or args.capture_probe or args.candidates_probe
            or args.maa_readiness_output or args.maa_queue_probe or args.queue_probe_vision
            or args.allow_game_input or args.allow_maa_import_test or args.ui_capture_probe):
        parser.error('--maa-queue-test cannot be combined with other diagnostics or input opt-ins')
    if args.maa_queue_test and not 1 <= args.maa_queue_timeout <= 3600:
        parser.error('--maa-queue-timeout must be between 1 and 3600 seconds')
    if args.queue_probe_vision and not args.maa_queue_probe:
        parser.error('--queue-probe-vision requires --maa-queue-probe')
    if args.maa_queue_probe and (args.self_test or args.conversation_test or args.execution_probe
            or args.task_dialogue_test or args.capture_probe or args.candidates_probe
            or args.maa_readiness_output or args.allow_game_input or args.allow_maa_import_test):
        parser.error('--maa-queue-probe is an independent read-only diagnostic')
    if args.allow_maa_import_test and not args.conversation_test:
        parser.error('--allow-maa-import-test requires --conversation-test')
    if args.task_dialogue_test:
        if (args.self_test or args.conversation_test or args.execution_probe or args.capture_probe
                or args.candidates_probe or args.maa_readiness_output or args.allow_game_input
                or args.allow_maa_import_test):
            parser.error('--task-dialogue-test cannot be combined with another diagnostic or input opt-in')
        if args.task_dialogue_timeout <= 0:
            parser.error('--task-dialogue-timeout must be positive')
    if args.candidates_probe:
        if args.candidates_output is None:parser.error('--candidates-output is required')
        from shu.providers import ProviderConfig
        from shu.research_tools import ResearchTools
        import time as probe_time
        output=args.candidates_output.resolve();output.mkdir(parents=True,exist_ok=True)
        started=probe_time.monotonic()
        research=ResearchTools(ProviderConfig(),output)
        try:
            result=research.candidates(args.candidates_probe)
            report={'mode':'developer public station probe; NOT model autonomy','stage':args.candidates_probe,
                'elapsed_seconds':round(probe_time.monotonic()-started,3),'result':result,
                'stats':research.stats,'model_requests':0,'game_inputs':0,'maa_inputs':0}
        except Exception as exc:
            report={'mode':'developer public station probe; NOT model autonomy','error':str(exc)[:500],
                    'model_requests':0,'game_inputs':0,'maa_inputs':0}
        (output/'candidate-probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        return 0 if report.get('result',{}).get('ok') and report['result'].get('coverage',{}).get('complete') else 2
    frozen = bool(getattr(sys, "frozen", False))
    app_root = Path(sys.executable).resolve().parent if frozen else Path(__file__).resolve().parent
    resource_root = Path(getattr(sys, "_MEIPASS", app_root))
    project_root=app_root.parent.parent if app_root.parent.name=='release' else app_root
    if args.verify_skin_vignette is not None:
        from shu.vignette_diagnostics import run_vignette_diagnostics
        return run_vignette_diagnostics(resource_root,args.data_dir,args.verify_skin_vignette)
    shared_profile=None
    if not args.self_test:
        try:
            location=json.loads((project_root/'workspace-profile.json').read_text(encoding='utf-8'))
            shared_data=(project_root/location['data_dir']).resolve()
            shared_key=(project_root/location['key_file']).resolve()
            if shared_data.is_relative_to(project_root.resolve()) and shared_key.is_relative_to(project_root.resolve()) and shared_data.is_dir():
                shared_profile=(shared_data,shared_key if shared_key.is_file() else project_root/'DeepSeek_API_Key.txt')
        except (OSError,ValueError,KeyError,TypeError):pass
    # Optional SDK is kept out of the baseline distribution. It is never downloaded on startup.
    runtime_paths = [app_root / "optional-runtime"]
    if not frozen:
        runtime_paths.append(app_root.parent / ".companion-runtime")
        runtime_paths.append(app_root.parent / ".capture-runtime")
        runtime_paths.append(app_root.parent / ".gui-runtime")
        runtime_paths.append(app_root.parent / ".agent-runtime")
    for runtime_path in runtime_paths:
        if runtime_path.is_dir():
            sys.path.insert(0, str(runtime_path))
    if args.maa_focus_test:
        from shu.maa_focus_acceptance import run_focus_acceptance
        return run_focus_acceptance(args.maa_focus_test,resource_root,version=APP_VERSION)
    if args.maa_battle_test:
        from shu.maa_battle_acceptance import run_battle_acceptance
        return run_battle_acceptance(project_root,resource_root,args.maa_battle_test,
            args.key_file or shared_profile[1],shared_profile[0],version=APP_VERSION,
            prior_focus_profile=args.prior_focus_profile,prior_focus_state_sha256=args.prior_focus_state_sha256,
            battle_import_report=args.battle_import_report)
    if args.task_overlap_live_test:
        from shu.task_overlap_acceptance import run_overlap_acceptance
        return run_overlap_acceptance(resource_root,args.task_overlap_live_test,
            args.key_file or shared_profile[1],args.data_dir or shared_profile[0],version=APP_VERSION)
    if args.maa_queue_test:
        from shu.maa_queue_acceptance import run_queue_acceptance
        return run_queue_acceptance(project_root,resource_root,args.maa_queue_test,
            args.key_file or (shared_profile[1] if shared_profile else project_root/'DeepSeek_API_Key.txt'),
            shared_profile[0] if shared_profile else project_root/'user-data',
            request_file=args.maa_queue_request_file,mode=args.maa_queue_mode,
            allow_queue_import=args.allow_maa_queue_import_test,timeout=args.maa_queue_timeout,
            version=APP_VERSION)
    if args.maa_queue_probe:
        from tools.maa_queue_probe import run_probe
        report = run_probe(args.maa_queue_probe,project_root=project_root,with_vision=args.queue_probe_vision)
        if sys.stdout:
            print(json.dumps({'passed':report['passed'],'code':report['code']},ensure_ascii=False))
        return 0 if report['passed'] else 2
    if args.task_dialogue_test:
        from shu.task_dialogue_acceptance import run_task_dialogue_acceptance
        return run_task_dialogue_acceptance(project_root,resource_root,args.task_dialogue_test,
            args.key_file or (shared_profile[1] if shared_profile else project_root/'DeepSeek_API_Key.txt'),
            shared_profile[0] if shared_profile else project_root/'user-data',
            timeout=args.task_dialogue_timeout,mode=args.task_dialogue_mode,version=APP_VERSION)
    if args.maa_readiness_output:
        from shu.maa_visual_import import probe_visual_import
        report = probe_visual_import(args.maa_readiness_output, wait_for_window=args.maa_readiness_wait)
        if sys.stdout:
            print(json.dumps({'passed': report['passed'], 'code': report['code']}, ensure_ascii=False))
        return 0 if report['passed'] else 2
    if args.conversation_test:
        if args.conversation_output is None:parser.error('--conversation-output required')
        from shu.conversation_acceptance import run_conversation_acceptance
        return run_conversation_acceptance(project_root,resource_root,args.conversation_output,args.conversation_test,
            args.key_file or (shared_profile[1] if shared_profile else project_root/'DeepSeek_API_Key.txt'),
            shared_profile[0] if shared_profile else project_root/'user-data', version=APP_VERSION,
            allow_maa_import=args.allow_maa_import_test,prepare_import_only=args.import_preparation_only,
            permission_handoff=args.maa_permission_handoff_test,
            allow_maa_queue_import=args.allow_maa_queue_conversation_test,
            allow_single_battle=args.allow_single_battle_conversation_test,
            single_battle_target=args.single_battle_target.rsplit(':',1) if args.single_battle_target else None,
            timeout_seconds=1200 if args.allow_single_battle_conversation_test else 420)
    if args.execution_probe:
        if not args.allow_game_input:
            parser.error('--execution-probe requires --allow-game-input')
        from shu.diagnostics import run_execution_probe
        return run_execution_probe(app_root,resource_root,
            (args.capture_output or app_root/'artifacts/execution-probe').resolve(),
            args.execution_probe,(args.key_file or app_root/'DeepSeek_API_Key.txt').resolve(),args.probe_click_limit,
            args.probe_start_delay_ms)
    if args.capture_probe:
        if args.ui_capture_probe:
            from shu.diagnostics import run_ui_capture_probe
            return run_ui_capture_probe(app_root,resource_root,
                        (args.capture_output or app_root/'artifacts/ui-capture-probe').resolve(),args.capture_probe)
        from shu.windows import WindowController,WindowError
        output=(args.capture_output or app_root/'artifacts/capture-probe').resolve()
        output.mkdir(parents=True,exist_ok=True)
        adapter=WindowController(output,prefer_maafw=False)
        report={'status':'failed','mode':'strict_background','game_inputs':0,'model_requests':0}
        started=time.monotonic()
        try:
            target=adapter.bind(args.capture_probe)
            before=int(adapter._native().user.GetForegroundWindow())
            report['persistent_session_started']=adapter.start_capture_session()
            observation=adapter.capture(background_only=True)
            after=int(adapter._native().user.GetForegroundWindow())
            report.update(status='passed',window=target.to_dict(),observation=observation,
                          foreground_before=before,foreground_after=after,activated_target=False,
                          foreground_unchanged=before==after,elapsed_seconds=round(time.monotonic()-started,3))
        except Exception as exc:
            report.update(error=str(exc) if isinstance(exc,WindowError) else type(exc).__name__,
                          code=getattr(exc,'code','capture_failed'))
        finally:
            adapter.stop_capture_session()
        (output/'capture-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if sys.stdout:print(json.dumps(report,ensure_ascii=True))
        return 0 if report['status']=='passed' else 1
    if args.self_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.pop("DEEPSEEK_API_KEY", None)

    from PySide6.QtCore import QLockFile, QTimer
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QMessageBox
    from shu.controller import SessionController
    from shu.companion_ui import CompanionShell

    app = QApplication(sys.argv[:1])
    app.setApplicationVersion(APP_VERSION)
    # Qt's offscreen plugin on Windows does not always enumerate native fonts.
    # Register the installed OS font for the offline visual check; no font is redistributed.
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen" and os.name == "nt":
        system_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            if (system_fonts / name).exists():
                QFontDatabase.addApplicationFont(str(system_fonts / name))
    app.setApplicationName("ShuCompanion")
    app.setOrganizationName("ShuCompanion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setQuitOnLastWindowClosed(False)

    data_dir = (args.data_dir or (shared_profile[0] if shared_profile else app_root / ("artifacts/self-test-data" if args.self_test else "data"))).resolve()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        QMessageBox.critical(None, "无法创建本地数据目录", "请将应用解压到可写入的文件夹后启动。")
        return 2
    lock = QLockFile(str(data_dir / "session.lock"))
    locked=lock.tryLock(100)
    if args.wait_for_profile:
        deadline=time.monotonic()+10
        while not locked and time.monotonic()<deadline:
            app.processEvents();locked=lock.tryLock(200)
    if not locked:
        QMessageBox.information(None, "黍已在运行", "同一个数据目录已有黍运行，请从托盘打开。")
        return 3

    # A self-test has its own empty key location, and cannot read the user's credential.
    controller_root = data_dir / "offline-root" if args.self_test else app_root
    controller_root.mkdir(parents=True, exist_ok=True)
    key_path = (args.key_file.resolve() if args.key_file and not args.self_test
                else shared_profile[1] if shared_profile else controller_root / "DeepSeek_API_Key.txt")
    if not key_path.exists():
        key_path.write_text(KEY_TEMPLATE, encoding="utf-8")
    controller = SessionController(controller_root, data_dir, key_path=key_path)
    window = CompanionShell(controller, resource_root)
    failures = []
    prior_hook = sys.excepthook

    def exception_hook(kind, value, traceback):
        failures.append(kind.__name__)
        if not args.self_test:
            # Credentials and exception text are deliberately not persisted.
            QMessageBox.critical(window, "本次操作未完成", "界面操作遇到异常。请保存当前工作并重新打开应用。")
        else:
            prior_hook(kind, value, traceback)

    sys.excepthook = exception_hook
    window.show_pet()
    if not args.self_test and hasattr(window.speech_output,'warm_up'):
        QTimer.singleShot(0,window.speech_output.warm_up)
    # Preserve old shortcut flags while directing them to the new pet panel.
    if args.activity or args.workbench:window.open_activity()
    startup_activity_visible=window.activity.isVisible()
    startup_host_visible=window.isVisible()

    def render():
        checked_skins=[]
        settings_checked=False
        if args.self_test:
            pet_path=data_dir.parent/'pet-startup-preview.png'
            pet_path.parent.mkdir(parents=True,exist_ok=True);window.pet.grab().save(str(pet_path))
            controller.submit("离线自检目标：刷六次，不使用理智药（尚未执行）", 2)
            # Only this isolated self-test profile is changed. All skin assets
            # come from the same catalog used by the normal pet entry.
            selected=window.pet.canvas.skin_id
            for skin in window.pet.skin_catalog.ordered():
                window.pet.canvas.set_skin(skin,animate=False)
                app.processEvents()
                skin_path=data_dir.parent/f'pet-{skin.id}-preview.png'
                skin_saved=window.pet.grab().save(str(skin_path))
                checked_skins.append({'skin_id':skin.id,'selected':window.pet.canvas.skin_id==skin.id,
                                      'preview_saved':skin_saved})
            if not checked_skins or not all(item['selected'] and item['preview_saved'] for item in checked_skins):
                failures.append('skin_self_test_failed')
            previous=window.pet.skin_catalog.get(selected)
            if previous is not None:window.pet.canvas.set_skin(previous,animate=False)
            window.open_settings('general')
            app.processEvents()
            settings_checked=window._settings_dialog is not None and window._settings_dialog.isVisible()
            if not settings_checked:failures.append('settings_self_test_failed')
            if window._settings_dialog is not None:
                window._settings_dialog.grab().save(str(data_dir.parent/'settings-preview.png'))
                window._settings_dialog.hide()
            window.open_activity()
        app.processEvents()
        destination = args.snapshot or (data_dir.parent / "activity-preview.png" if args.self_test else None)
        saved = None
        if destination:
            destination = destination.resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            saved = window.activity.grab().save(str(destination))
            if not saved:
                failures.append("snapshot_failed")
        if args.self_test:
            report = {"offline": True, "network_requests": 0, "game_inputs": 0,
                      "snapshot_saved": saved, "settings_checked":settings_checked,
                      "skin_checks":checked_skins,
                      "task_status": controller.current_task()["status"],
                      "confirmed_runs": controller.current_task()["completed_runs"],
                      "startup_surface": "pet", "host_initially_visible":startup_host_visible,
                      "activity_initially_visible":startup_activity_visible,
                      "legacy_workbench_present":False,
                      "cursor_following_enabled": window.pet.canvas.follow_cursor,
                      "ui_exceptions": failures}
            (data_dir.parent / "self-test.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=True))
            QTimer.singleShot(100, window.quit_application)

    if args.self_test or args.snapshot:
        QTimer.singleShot(500, render)
    if args.quit_after_ms:
        QTimer.singleShot(max(100, args.quit_after_ms), window.quit_application)
    try:
        result = app.exec()
    finally:
        controller.close()
        lock.unlock()
        sys.excepthook = prior_hook
    return 1 if failures else result


if __name__ == "__main__":
    # Frozen GUI helpers must enter multiprocessing bootstrap before parsing
    # the desktop application's command line or creating another pet window.
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())

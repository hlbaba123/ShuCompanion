"""Explicit read-only live acceptance through the pet's text-submission signal.

Uses the existing configured Key path without copying/printing its contents.
All appearance and test data are isolated; no game or MAA input is permitted.
"""
from datetime import datetime, timedelta, timezone
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name in ('.capture-runtime', '.gui-runtime', '.agent-runtime'):
    path = ROOT.parent / name
    if path.is_dir(): sys.path.insert(0, str(path))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=180)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or not output.is_relative_to((ROOT/'artifacts').resolve()):
        raise SystemExit('Use a fresh output under project artifacts')
    output.mkdir(parents=True)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ['TEMP'] = os.environ['TMP'] = str(ROOT.parent/'.pet-temp')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    from shu.controller import SessionController
    from shu.companion_ui import CompanionShell
    from shu.skins import save_skin_preference
    app = QApplication([]); app.setQuitOnLastWindowClosed(False)
    fonts = Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    for name in ('msyh.ttc', 'msyhbd.ttc'):
        if (fonts/name).exists(): QFontDatabase.addApplicationFont(str(fonts/name))
    app.setFont(QFont('Microsoft YaHei UI', 10))
    profile = json.loads((ROOT/'workspace-profile.json').read_text(encoding='utf-8'))
    data_path, key_path = [(ROOT/profile[k]).resolve() for k in ('data_dir','key_file')]
    if not all(p.is_relative_to(ROOT) for p in (data_path,key_path)):
        raise SystemExit('Profile is outside the project')
    data = output/'data'; data.mkdir()
    settings = json.loads((data_path/'settings.json').read_text(encoding='utf-8'))
    selected = {k:settings[k] for k in ('model','base_url','game_model','game_thinking') if k in settings}
    (data/'settings.json').write_text(json.dumps(selected),encoding='utf-8')
    (data/'voice-output.json').write_text('{"enabled":false}',encoding='utf-8')
    save_skin_preference(data/'pet-appearance.json','purple')
    controller = SessionController(ROOT,data,key_path=key_path)
    shell = CompanionShell(controller,ROOT)
    question = '黍，查一下明日方舟国服当前开放的作战活动，告诉我能打的阶段和开放时间，附上来源。'
    messages, errors = [], []
    controller.message.connect(lambda role,text: messages.append({'role':role,'text':text}))
    controller.error.connect(errors.append)
    clock = lambda: datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    start, started_at = time.monotonic(), clock()
    initial = {'host_hidden':not shell.isVisible(),'activity_hidden':not shell.activity.isVisible(),
               'settings_closed':shell._settings_dialog is None,'skin_id':shell.pet.canvas.skin_id}
    captures = {}
    def snapshot(widget,name):
        captures[name] = widget.grab().save(str(output/(name+'.png')))
    try:
        shell.show_pet(); app.processEvents(); snapshot(shell.pet,'pet-purple')
        shell.open_settings('general'); app.processEvents(); snapshot(shell._settings_dialog,'settings')
        shell._settings_dialog.hide()
        # Opening the panel is explicit test UI inspection, never a task action.
        shell.open_activity(); app.processEvents()
        shell.pet.text_submitted.emit(question)
        during = False
        while controller.is_busy and time.monotonic()-start < args.timeout:
            app.processEvents(); time.sleep(.02)
            if not during and any(e.get('stage')=='tool_started' for e in shell.activity.events):
                snapshot(shell.activity,'activity-during'); during=True
        app.processEvents()
        timed_out = controller.is_busy
        if timed_out: controller.stop(silent=True)
        snapshot(shell.activity,'activity-after')
        answer = '\n\n'.join(m['text'] for m in messages if m['role']=='assistant')
        report = {'started_at':started_at,'completed_at':clock(),'question':question,
            'elapsed_seconds':round(time.monotonic()-start,3),'timed_out':timed_out,'errors':errors,
            'initial':initial,'events':list(shell.activity.events),
            'network':controller.last_network_report,'answer':answer,'captures':captures,
            'input_permission':controller.game_input_allowed,'screen_permission':controller.screen_capture_allowed,
            'created_tasks':len(controller.engine.list_tasks()),'model_metrics':list(controller.client.metrics_history),
            'final_skin_id':shell.pet.canvas.skin_id,'host_stayed_hidden':not shell.isVisible()}
        ok = bool(answer and not errors and not timed_out and report['network'].get('sources')
            and report['created_tasks']==0 and not report['input_permission'] and not report['screen_permission']
            and any(e.get('role')=='research' for e in report['events']) and all(captures.values()))
        report['status'] = 'passed' if ok else 'needs_review'
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        (output/'answer.md').write_text(answer or '未取得完整回答',encoding='utf-8')
        print(json.dumps({'status':report['status'],'elapsed_seconds':report['elapsed_seconds'],
            'event_count':len(report['events']),'captures':captures,'report':str(output/'report.json')},ensure_ascii=False))
        return 0 if ok else 1
    finally:
        shell.quit_application(); controller.close()


if __name__ == '__main__': raise SystemExit(main())

"""Explicit, isolated natural-language acceptance for one loaded MAA job."""
from pathlib import Path
import json
import os
import time


def run_battle_acceptance(root,resources,output,key,source,*,version,request=None,
                          prior_focus_profile=None,prior_focus_state_sha256=None,battle_import_report=None):
    from dataclasses import asdict
    from .battle_import_binding import load_import_binding,ImportBindingError
    output=Path(output).resolve();source=Path(source).resolve()
    if output.exists() or output.is_relative_to(source):raise ValueError('Use a new isolated output directory')
    output.mkdir(parents=True)
    try:expected_job=load_import_binding(battle_import_report) if battle_import_report is not None else None
    except ImportBindingError as error:
        (output/'report.json').write_text(json.dumps({'version':version,'passed':False,'gui_inputs':0,
            'battle_start_submitted':False,'errors':[str(error)],'code':error.code,
            'scope':'import-to-battle binding rejected before any controller/model/input'},
            ensure_ascii=False,indent=2),encoding='utf-8')
        return 2
    from PySide6.QtWidgets import QApplication
    from .controller import SessionController
    from .task_dialogue_acceptance import source_box
    from .maa_facts import RosterStore
    data=output/'data';data.mkdir()
    settings=json.loads((source/'settings.json').read_text(encoding='utf-8'))
    (data/'settings.json').write_text(json.dumps({k:settings[k] for k in
        ('model','base_url','game_model','game_thinking','intent_thinking','leader_thinking') if k in settings}),encoding='utf-8')
    (data/'voice-output.json').write_text('{"enabled":false}',encoding='utf-8')
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False);app.setApplicationVersion(version)
    c=SessionController(Path(resources),data,key_path=Path(key))
    if expected_job is not None:c.execution.battle_expected_job=expected_job
    if prior_focus_profile is not None:
        c.execution.focus_recovery_profile=Path(prior_focus_profile).resolve(strict=True)
        c.execution.focus_recovery_state_sha256=prior_focus_state_sha256
    box,_=source_box(source)
    RosterStore(c.execution.store).publish('local-default',box['operators'],box['complete'],box['source'],
        explicit_ownership=box.get('explicit_ownership',False))
    c.screen_capture_allowed=c.game_input_allowed=True
    c.execution.control_timeout_seconds=60
    report={'version':version,'request':request or '开始 MAA 自动战斗一次：核对当前已导入的单作业，导航到对应关卡的开始行动页面后启动MAA；不用药、不用源石，不点击游戏开始行动，由MAA操作战斗。',
            'scope':'one currently loaded Copilot, real model navigation and MAA start; no developer-chosen coordinates or job codes',
            'messages':[],'errors':[],'routes':[],'results':[],'passed':False}
    if expected_job is not None:report['import_binding']=asdict(expected_job)
    c.message.connect(lambda role,text:report['messages'].append({'role':role,'text':text}))
    c.error.connect(report['errors'].append);c.message_routed.connect(report['routes'].append)
    c.execution.finished.connect(report['results'].append)
    def save():
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def event(value):
        with (output/'progress.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
        save()
    c.activity_progress.connect(event)
    start=time.monotonic()
    try:
        save();c.set_input_target(None,mode='new');c.submit(report['request'])
        idle_since=None
        while time.monotonic()-start<1200:
            app.processEvents();time.sleep(.02)
            idle=not c.is_busy and not c.execution.active and not c.dialogue.pending
            if idle:
                idle_since=idle_since or time.monotonic()
                if time.monotonic()-idle_since>.7:break
            else:idle_since=None
        report['timed_out']=c.is_busy or c.execution.active
        if report['timed_out']:c.stop(silent=True)
        ids=list(dict.fromkeys(v['task_id'] for v in report['routes'] if v.get('task_id')))
        report['tasks']={tid:c.execution.store.current(tid) for tid in ids}
        with c.engine._lock:
            report['operations']=[dict(row) for row in c.engine._db.execute('SELECT operation_id,task_id,starts,stop_only,status,submitted_at,result FROM workflow_operations')]
            report['dispatch_results']=[dict(row) for row in c.engine._db.execute('SELECT d.role,r.status,r.result FROM workflow_results r JOIN workflow_dispatches d ON d.dispatch_id=r.dispatch_id')]
        report['elapsed_seconds']=round(time.monotonic()-start,3)
        starts=[op for op in report['operations'] if op['starts'] and op['submitted_at']]
        report['passed']=(len(starts)==1 and not report['errors'] and not report['timed_out']
            and len(ids)==1 and report['tasks'][ids[0]]['status']=='completed'
            and bool(json.loads(starts[0]['result'] or '{}').get('battle_idle_verified')))
        save();return 0 if report['passed'] else 2
    except Exception as error:
        report['fatal_error']={'type':type(error).__name__,'message':str(error)[:600]};save();raise
    finally:
        c.close()

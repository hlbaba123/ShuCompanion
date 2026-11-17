"""Paid, explicit Leader acceptance. GUI/game inputs stay disabled throughout.

Part A uses real persona inference and durable contracts with a recording-only
submission adapter. Part B runs the real four-role graph for a read-only public
research objective. Neither part is a battle or MAA import acceptance.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
for folder in ('.agent-runtime','.capture-runtime','.gui-runtime'):
    path=ROOT.parent/folder
    if path.is_dir():sys.path.insert(0,str(path))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--graph-only',action='store_true')
    args=parser.parse_args(); output=args.output.resolve()
    if output.exists() or not output.is_relative_to((ROOT/'artifacts').resolve()):
        raise SystemExit('Use a new output directory under artifacts')
    output.mkdir(parents=True)
    os.environ['TEMP']=os.environ['TMP']=str(ROOT.parent/'.pet-temp')
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont,QFontDatabase
    from shu.controller import SessionController
    from shu.companion_ui import CompanionShell
    from shu.skins import save_skin_preference
    from shu.leader_policy import normalize_task_spec
    app=QApplication([]);app.setQuitOnLastWindowClosed(False)
    fonts=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    for name in ('msyh.ttc','msyhbd.ttc'):
        if (fonts/name).exists():QFontDatabase.addApplicationFont(str(fonts/name))
    app.setFont(QFont('Microsoft YaHei UI',10))
    profile=json.loads((ROOT/'workspace-profile.json').read_text(encoding='utf-8'))
    key=(ROOT/profile['key_file']).resolve(); original=(ROOT/profile['data_dir']).resolve()
    if not key.is_relative_to(ROOT) or not original.is_relative_to(ROOT):raise SystemExit('Invalid profile location')
    original_settings=json.loads((original/'settings.json').read_text(encoding='utf-8'))
    clock=lambda:datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
    def make(tag):
        data=output/tag;data.mkdir()
        selected={k:original_settings[k] for k in ('model','base_url','game_model','game_thinking') if k in original_settings}
        (data/'settings.json').write_text(json.dumps(selected),encoding='utf-8')
        (data/'voice-output.json').write_text('{"enabled":false}',encoding='utf-8')
        save_skin_preference(data/'pet-appearance.json','purple')
        c=SessionController(ROOT,data,key_path=key)
        shell=CompanionShell(c,ROOT);shell.show_pet()
        return c,shell
    def wait(predicate,seconds):
        end=time.monotonic()+seconds
        while predicate() and time.monotonic()<end:
            app.processEvents();time.sleep(.02)
        app.processEvents()
        return not predicate()
    report={'started_at':clock(),'scope':'real intent inference + read-only real multi-agent graph; no game execution'}
    c,shell=make('intent-data')
    errors=[]; c.error.connect(errors.append)
    received=[]
    def record(goal,contract,*,modify=False,request_cancel=None):
        if request_cancel and request_cancel.is_set():raise RuntimeError('Cancelled before recording')
        state=c.execution.store.submit(goal,copy.deepcopy(contract),
            task_id=c.execution.current_task_id if modify else None)
        c.execution.current_task_id=state['task_id'];received.append(copy.deepcopy(state['contract']))
        return {'accepted':True,'recording_only':True,'game_input_sent':False,'battle_start_submitted':False}
    c.execution.submit=record
    turns=[]
    questions=[
        '当前目标：查询明日方舟 TL-10 关卡的 MAA 作业并导入，随后开始自动战斗，不使用理智药、不花源石。',
        '刷六次，不使用理智药',
        '改成八次，仍然不花源石',
        '这一局结束后就停',
    ]
    try:
        for text in ([] if args.graph_only else questions):
            started=time.monotonic();before=len(errors);c.last_intent_contract=None
            shell.pet.text_submitted.emit(text)
            done=wait(lambda:c.is_busy,150)
            if not done:c.stop(silent=True)
            turns.append({'question':text,'elapsed_seconds':round(time.monotonic()-started,3),
                'contract':c.last_intent_contract,'errors':errors[before:],'timed_out':not done})
            print(json.dumps({'part':'intent','question':text,'contract':c.last_intent_contract,
                'errors':errors[before:]},ensure_ascii=False),flush=True)
        shell.open_activity();app.processEvents()
        shell.activity.grab().save(str(output/'history-current.png'))
        records=shell.activity._history.records()
        if records:shell.activity.select_request(records[-1]['request_id'])
        shell.activity.grab().save(str(output/'history-first.png'))
        checks=[]
        for index,turn in enumerate(turns):
            contract=turn['contract'] or {}; spec=contract.get('task_spec') or {}
            checks.append(not turn['errors'] and not turn['timed_out'] and spec.get('stage_code')=='TL-10'
                and spec.get('medicine_limit')==0 and spec.get('premium_limit')==0
                and (index not in (1,2) or spec.get('repeat_count')==(6 if index==1 else 8))
                and (index!=3 or spec.get('stop_after_current') is True))
        report['intent']={'mode':'skipped; validate in prior report' if args.graph_only else 'real model + recording-only submission adapter','turns':turns,'checks':checks,
            'passed':all(checks),'history_records':len(records),'input_permission':c.game_input_allowed,
            'screen_permission':c.screen_capture_allowed,'model_metrics':list(c.client.metrics_history)}
    finally:shell.quit_application();c.close()

    c,shell=make('graph-data'); errors=[]; c.error.connect(errors.append)
    from shu.maa_agents import MaaRoleAgents
    observed_roles=[]
    def role_factory(*args,**kwargs):
        roles=MaaRoleAgents(*args,**kwargs);observed_roles.append(roles);return roles
    c.execution.role_factory=role_factory
    question='只读核实明日方舟国服当前开放的游戏内作战活动，给出公告来源和已开放阶段；不导入、不启动MAA。'
    spec=normalize_task_spec({'kind':'lookup'},user_request=question,goal=question,intent='execute')
    contract={'intent':'execute','user_request':question,'goal':question,'constraints':['只读，不操作MAA'],
        'completion_criteria':['资料含来源并区分已开放与未开放阶段'],'task_spec':spec.as_dict()}
    events=[];results=[]
    c.activity_progress.connect(events.append)
    c.execution.finished.connect(results.append)
    started=time.monotonic()
    try:
        c.message.emit('user',question)
        outcome=c.execution.submit(question,contract)
        c._task_id=outcome['task_id']
        done=wait(lambda:c.execution.active,240)
        if not done:c.stop(silent=True)
        shell.open_activity();app.processEvents()
        shell.activity.grab().save(str(output/'graph-history.png'))
        task=c.current_task()
        answer=results[-1].get('summary','') if results else ''
        steps=[{'role':e.get('role'),'stage':e.get('stage'),'tool':e.get('tool'),'summary':e.get('summary')}
               for e in events if e.get('stage') in ('dispatch_started','tool_started','task_finished','task_waiting')]
        report['graph']={'mode':'real LangGraph + isolated real role clients + actual read-only public tools',
            'question':question,'elapsed_seconds':round(time.monotonic()-started,3),'timed_out':not done,
            'errors':errors,'task':task,'results':results,'steps':steps,'events':events,
            'input_permission':c.game_input_allowed,'screen_permission':c.screen_capture_allowed,
            'role_metrics':{role:list(client.metrics_history) for roles in observed_roles for role,client in roles.clients.items()},
            'passed':bool(done and not errors and results and results[-1].get('status')=='research_verified'
                          and task and task['status']=='completed' and task['completed_runs']==0)}
        (output/'graph-answer.md').write_text(answer or '未形成成功结果，见report.json',encoding='utf-8')
    finally:shell.quit_application();c.close()
    report['completed_at']=clock()
    report['passed']=report['intent']['passed'] and report['graph']['passed']
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'intent_passed':report['intent']['passed'],
        'graph_passed':report['graph']['passed'],'report':str(output/'report.json')},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1

if __name__=='__main__':raise SystemExit(main())

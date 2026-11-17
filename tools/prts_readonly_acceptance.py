"""Real pet -> intent -> graph -> PRTS lookup, with GUI permissions disabled.

No candidate ID or answer is supplied. All discovery is performed by Shu's
role clients and registered public tools; artifacts are isolated from user data.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
for name in ('.agent-runtime','.capture-runtime','.gui-runtime'):
    sys.path.insert(0,str(ROOT.parent/name))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--stage',default='TL-6')
    parser.add_argument('--bulk',action='store_true',help='Require all available candidate summaries and one complete JSON')
    parser.add_argument('--graph-only',action='store_true',help='Explicitly skip persona to isolate real role/tool verification')
    args=parser.parse_args()
    import re
    if not re.fullmatch(r'[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+',args.stage):parser.error('Invalid stage')
    output=args.output.resolve()
    if output.exists() or not output.is_relative_to((ROOT/'artifacts').resolve()):
        raise SystemExit('Use a fresh artifacts directory')
    output.mkdir(parents=True)
    os.environ['TEMP']=os.environ['TMP']=str(ROOT.parent/'.pet-temp')
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont,QFontDatabase
    from shu.controller import SessionController
    from shu.companion_ui import CompanionShell
    from shu.maa_agents import MaaRoleAgents
    from shu.skins import save_skin_preference
    app=QApplication([]);app.setQuitOnLastWindowClosed(False)
    for name in ('msyh.ttc','msyhbd.ttc'):
        QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name))
    app.setFont(QFont('Microsoft YaHei UI',10))
    profile=json.loads((ROOT/'workspace-profile.json').read_text(encoding='utf-8'))
    original=(ROOT/profile['data_dir']).resolve();key=(ROOT/profile['key_file']).resolve()
    if not original.is_relative_to(ROOT) or not key.is_relative_to(ROOT):raise SystemExit('Invalid profile')
    settings=json.loads((original/'settings.json').read_text(encoding='utf-8'))
    data=output/'data';data.mkdir()
    (data/'settings.json').write_text(json.dumps({k:settings[k] for k in
        ('model','base_url','game_model','game_thinking') if k in settings}),encoding='utf-8')
    (data/'voice-output.json').write_text('{"enabled":false}',encoding='utf-8')
    save_skin_preference(data/'pet-appearance.json','purple')
    c=SessionController(ROOT,data,key_path=key);shell=CompanionShell(c,ROOT)
    roles=[];events=[];results=[];errors=[]
    def role_factory(*args,**kwargs):
        role=MaaRoleAgents(*args,**kwargs);roles.append(role);return role
    c.execution.role_factory=role_factory
    c.activity_progress.connect(events.append);c.execution.finished.connect(results.append);c.error.connect(errors.append)
    question=f'只读查询明日方舟{args.stage}关卡的MAA作业，获取至少一份对应关卡的完整JSON和神秘代码，列出来源。本轮不筛选干员、不匹配阵容、不导入MAA、不操作游戏。'
    if args.bulk:
        question=f'只读收集明日方舟{args.stage}关卡在PRTS Plus的全部可取得作业摘要，保存候选池供后续筛选；按需下载一份完整JSON核对关卡并给出神秘代码与来源。说明总数、已取得数量和是否取齐。本轮不筛选干员、不匹配阵容、不导入MAA、不操作游戏。'
    started=time.monotonic()
    report={'started_at':datetime.now(timezone.utc).isoformat(),'question':question,
            'mode':('direct explicit read-only contract + real role clients and public station tools; persona NOT tested'
                    if args.graph_only else 'real pet controller + real DeepSeek intent and roles + public station tools')}
    try:
        shell.show_pet()
        if args.graph_only:
            from shu.leader_policy import normalize_task_spec
            spec=normalize_task_spec({'kind':'lookup'},user_request=question,goal=question,intent='execute')
            contract={'intent':'execute','user_request':question,'goal':question,'constraints':['只读，不操作MAA'],
                      'completion_criteria':['取得对应关卡的完整JSON与可追溯神秘代码'],'task_spec':spec.as_dict()}
            c.message.emit('user',question)
            outcome=c.execution.submit(question,contract);c._task_id=outcome['task_id']
        else:shell.pet.text_submitted.emit(question)
        deadline=started+420
        while time.monotonic()<deadline:
            app.processEvents();time.sleep(.02)
            if not c.is_busy and not c.execution.active and (results or errors or time.monotonic()-started>5):break
        timed_out=bool(c.is_busy or c.execution.active)
        if timed_out:c.stop(silent=True)
        app.processEvents();shell.open_activity();app.processEvents()
        shell.activity.grab().save(str(output/'progress.png'))
        task=c.current_task();last=results[-1] if results else {}
        research=last.get('role_results',{}).get('research',{})
        downloads=research.get('verified_copilots',[])
        checked=[]
        for item in downloads:
            path=Path(item['path']).resolve()
            if not path.is_relative_to(data):raise ValueError('Download escaped isolated profile')
            raw=path.read_bytes();body=json.loads(raw.decode('utf-8-sig'))
            checked.append({**item,'hash_matches':hashlib.sha256(raw).hexdigest()==item['sha256'],
                'json_stage_name':body.get('stage_name'),'action_count':len(body.get('actions',[]))})
        pools=[]
        for pool in research.get('candidate_pools',[]):
            ref=pool['pool_ref'];path=Path(ref['path']).resolve()
            if not path.is_relative_to(data):raise ValueError('Pool escaped isolated profile')
            raw=path.read_bytes();body=json.loads(raw)
            pools.append({**pool,'hash_matches':hashlib.sha256(raw).hexdigest()==ref['sha256'],
                'saved_count':len(body['candidates']),'sources':body.get('sources',[])})
        metrics={role:list(client.metrics_history) for r in roles for role,client in r.clients.items()}
        all_metrics=[*list(c.client.metrics_history),*[m for rows in metrics.values() for m in rows]]
        usage={k:sum(m.get('usage',{}).get(k,0) for m in all_metrics) for k in
               ('prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens')}
        report.update(elapsed_seconds=round(time.monotonic()-started,3),contract=c.last_intent_contract,
            task=task,results=results,errors=errors,timed_out=timed_out,events=events,downloads=checked,
            role_metrics=metrics,intent_metrics=list(c.client.metrics_history),usage=usage,
            candidate_pools=pools,model_calls=len(all_metrics),
            input_permission=c.game_input_allowed,screen_permission=c.screen_capture_allowed,
            passed=bool(not timed_out and not errors and last.get('status')=='research_verified'
                and task and task['status']=='completed' and task['completed_runs']==0
                and checked and all(x['hash_matches'] and x['action_count']>0 and x['code'].upper()==args.stage.upper() for x in checked)
                and (not args.bulk or bool(pools) and all(p['hash_matches'] and p['coverage']['complete']
                    and p['saved_count']==p['coverage']['total'] for p in pools))
                and not metrics.get('maa') and not c.game_input_allowed and not c.screen_capture_allowed))
        (output/'answer.md').write_text(last.get('summary') or '未形成成功结果，见report.json',encoding='utf-8')
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:report[k] for k in ('passed','elapsed_seconds','errors','timed_out','downloads')},ensure_ascii=False),flush=True)
        return 0 if report['passed'] else 1
    finally:shell.quit_application();c.close()


if __name__=='__main__':raise SystemExit(main())

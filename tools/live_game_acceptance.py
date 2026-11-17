"""Single-phase live acceptance through the production controller.

Default preparation is metadata-only: no model, screenshot or input. Execution
requires --allow-game-input and a local Key path. This tool never approves UAC,
changes game files, reads game memory or certifies autonomous battle ability.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=('base','stage'),required=True)
    parser.add_argument('--goal',required=True,help='Complete goal, including stage/deployment mode when relevant')
    parser.add_argument('--key-file',type=Path,help='Existing local Key; never copied into reports')
    parser.add_argument('--output',type=Path,required=True,help='Independent acceptance output directory')
    parser.add_argument('--prepare-only',action='store_true',help='Metadata only, even if --allow-game-input is present')
    parser.add_argument('--allow-game-input',action='store_true',help='Explicitly execute this single authorized phase')
    parser.add_argument('--expected-game-pid',type=int)
    parser.add_argument('--input-attempt-cap',type=int,choices=range(1,33),default=32)
    parser.add_argument('--stop-file',type=Path,help='Request production stop by creating this file; default OUTPUT/STOP.flag')
    parser.add_argument('--show-pet',action='store_true',help='Opt in to an extra diagnostic pet; default creates no windows')
    parser.add_argument('--start-delay-seconds',type=float,default=6.)
    return parser


def phase_goal(phase,goal):
    if not isinstance(goal,str) or not goal.strip() or len(goal)>12000 or '\x00' in goal:
        raise ValueError('Provide a nonempty bounded goal.')
    scope=('仅收取已经生成的基建产物，不换班、不使用无人机加速、不调整配方，不进入关卡。'
           if phase=='base' else
           '仅运行一个已解锁关卡并等待本次结算，不重复开局；只可消耗现有普通理智一次。'
           '不确定关卡、阵容或需要额外资源时停止。若使用代理作战，必须明确记录为代理复现，不能声称自主部署。')
    return (goal+'\n\n本次单阶段验收边界：'+scope+
        '不使用理智药或源石补充理智，不购买、不寻访、不充值、不更改账号。'
        '不修改游戏文件、不读进程内存、不注入、不使用内部协议。'
        '按实际截图逐步验证，不把发送输入当完成。结束后停止本任务。')


def redact(value):
    if isinstance(value,dict):
        blocked={'api_key','authorization','key_file','reasoning','reasoning_content','hidden_reasoning',
                 'image_base64','image_data_url','sample_rgb','bgra','frame_buffer','provider_payload','raw_response'}
        return {str(key):redact(item) for key,item in value.items() if str(key).lower() not in blocked}
    if isinstance(value,(list,tuple)):return [redact(item) for item in value]
    if isinstance(value,(bytes,bytearray,memoryview)):return '[binary omitted]'
    if isinstance(value,str):
        value=re.sub(r'\bsk-[A-Za-z0-9_.-]{6,}','[Key hidden]',value)
        return re.sub(r'(?i)Bearer\s+\S+','Bearer [hidden]',value)
    return value


def usage_summary(calls):
    """Sum reported counters only. Missing API usage is not a measured zero."""
    totals={};roles={};missing=0
    def add(value,path=''):
        if not isinstance(value,dict):return
        for key,item in value.items():
            name=path+'.'+key if path else key
            if isinstance(item,int) and not isinstance(item,bool) and item>=0:totals[name]=totals.get(name,0)+item
            elif isinstance(item,dict):add(item,name)
    for call in calls:
        role=call.get('role','unknown');roles[role]=roles.get(role,0)+1
        usage=(call.get('metrics') or {}).get('usage')
        if not isinstance(usage,dict) or not usage:missing+=1
        else:add(usage)
    latency=[c.get('metrics',{}).get('latency_seconds') for c in calls]
    latency=[v for v in latency if isinstance(v,(int,float)) and not isinstance(v,bool)]
    return {'recorded_calls':len(calls),'calls_by_role':roles,'reported_usage_totals':totals,
            'calls_without_reported_usage':missing,'usage_complete':missing==0,
            'summed_latency_seconds':round(sum(latency),6) if latency else None,
            'price_or_cost_estimated':False}


def prepare_metadata(adapter,inspect_permission,expected_pid=None):
    clients=[window for window in adapter.list_windows(game_only=True) if window.is_game_client]
    if len(clients)!=1:
        return {'status':'blocked','error_code':'game_not_unique','message':'需要唯一真实游戏客户端。','model_requests':0,'game_inputs':0}
    window=clients[0]
    permission=inspect_permission(window)
    result={'status':'prepared','window':window.to_dict(),'permission':permission,'model_requests':0,'game_inputs':0}
    if expected_pid is not None and window.pid!=expected_pid:
        result.update(status='blocked',error_code='unexpected_game_pid',message='游戏进程与指定验收进程不一致。')
    elif window.minimized:
        result.update(status='blocked',error_code='minimized',message='请先恢复游戏窗口；准备模式不会切换窗口。')
    elif permission.get('requires_elevation'):
        result.update(status='blocked',error_code='elevation_required',message=permission.get('message','需要用户自行确认管理员启动。'))
    elif permission.get('own_elevated') is None:
        result.update(status='blocked',error_code='permission_unknown',message='无法确认本程序权限，不开始输入。')
    return result


class InputBudget:
    def __init__(self,cap=32):
        self.cap=cap;self.attempts=[];self.results=[];self._lock=threading.Lock()
    def wrap(self,kind,callback):
        def bounded(*args,**kwargs):
            from shu.windows import WindowError
            with self._lock:
                if len(self.attempts)>=self.cap:raise WindowError('已达到本阶段输入尝试上限，停止且不自动重试。','acceptance_input_limit')
                record={'number':len(self.attempts)+1,'kind':kind,'coordinates':list(args[:-1]),
                        'observation_id':args[-1] if args else None,'attempted_at':time.time()}
                self.attempts.append(record)
            try:
                result=callback(*args,**kwargs)
            except Exception as error:
                with self._lock:record.update(status='rejected',error_code=getattr(error,'code','input_not_completed'))
                raise
            with self._lock:
                record.update(status='submitted');self.results.append({'kind':kind,'result':copy.deepcopy(result)})
            return result
        return bounded
    def snapshot(self):
        with self._lock:return {'attempts':copy.deepcopy(self.attempts),'submitted':copy.deepcopy(self.results)}


class StopFileGate:
    """A one-shot local request; it never generates input or removes the file."""
    def __init__(self,path,on_stop):self.path=Path(path);self.on_stop=on_stop;self.triggered=False
    def poll(self):
        if self.triggered or not self.path.is_file():return False
        self.triggered=True;self.on_stop();return True


def submit_initial_goal(controller,goal,*,stop_requested=False,stopping=False,finishing=False):
    """Late screenshots must not restart a stopped or closed production session."""
    if stop_requested or stopping or finishing or getattr(controller,'_closed',False):return False
    controller.submit(goal)
    return True


def close_acceptance(controller,app,window=None):
    """Headless acceptance must not create pet bubbles or steal the game area."""
    if window is not None:window.quit_application()
    else:
        controller.close()
        app.quit()


def write_json(path,value):
    path=Path(path);temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(redact(value),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(path)


def run(args):
    sys.path[:0]=[str(ROOT),str(ROOT.parent/'.capture-runtime')]
    from shu.windows import WindowController
    from shu.permissions import inspect_game_input_permission
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    goal=phase_goal(args.phase,args.goal)
    execute=args.allow_game_input and not args.prepare_only
    if not 0<=args.start_delay_seconds<=30:raise ValueError('Preparation delay must be 0..30 seconds.')
    report={'phase':args.phase,'goal':goal,'user_goal':args.goal,'mode':'single_phase_live' if execute else 'metadata_prepare_only',
            'isolated_profile':True,'input_attempt_cap':args.input_attempt_cap,'status':'preparing',
            'resources_review':{'status':'not_independently_reviewed','before':None,'after':None},
            'battle_review':{'status':'not_independently_reviewed','autonomous_deployment_verified':False,
                             'trusted_battle_count_added':0,'execution_mode':'undetermined'},
            'model_requests':0,'game_inputs':0,'errors':[]}
    stop_file=(args.stop_file or output/'STOP.flag').resolve()
    report['stop_file']=str(stop_file)
    adapter=WindowController(output/'read-only-probe',prefer_maafw=False)
    try:metadata=prepare_metadata(adapter,inspect_game_input_permission,args.expected_game_pid)
    except Exception as error:
        metadata={'status':'blocked','error_code':getattr(error,'code','window_metadata_failed'),
                  'message':'无法完成只读客户端检查，未开始模型或输入。','model_requests':0,'game_inputs':0}
    report['preparation']=metadata
    if metadata['status']=='blocked' or not execute:
        report['status']=metadata['status'];write_json(output/'acceptance-report.json',report)
        write_json(output/'progress.json',report)
        return 2 if metadata['status']=='blocked' else 0
    if not args.key_file or not args.key_file.is_file():
        report.update(status='blocked',error_code='key_file_missing');write_json(output/'acceptance-report.json',report);return 2
    if (output/'data').exists():
        raise ValueError('Use a new output directory; live acceptance never reuses old task data.')

    from PySide6.QtCore import QLockFile,QTimer,Signal,QObject
    from PySide6.QtWidgets import QApplication
    from shu.controller import SessionController
    app=QApplication.instance() or QApplication(sys.argv[:1]);app.setQuitOnLastWindowClosed(False)
    lock=QLockFile(str(output/'acceptance.lock'))
    if not lock.tryLock(100):raise ValueError('Another acceptance run owns this output directory.')
    controller=SessionController(ROOT,output/'data',key_path=args.key_file.resolve())
    window=None
    if args.show_pet:
        from shu.companion_ui import CompanionShell
        window=CompanionShell(controller,ROOT);window.show_pet()
    report['diagnostic_pet_visible']=bool(args.show_pet)
    budget=InputBudget(args.input_attempt_cap)
    controller.windows.controlled_click=budget.wrap('click',controller.windows.controlled_click)
    controller.windows.controlled_drag=budget.wrap('drag',controller.windows.controlled_drag)
    report.update(messages=[],events=[],notices=[],started_at=time.time())
    controller.message.connect(lambda role,text:report['messages'].append({'role':role,'text':text,'at':time.time()}))
    controller.error.connect(report['errors'].append)
    controller.notice.connect(lambda text:report['notices'].append({'text':text,'at':time.time()}))
    controller.execution.event.connect(lambda kind,message,details:report['events'].append(
        {'kind':kind,'message':message,'details':copy.deepcopy(details),'at':time.time()}))
    started=time.monotonic();stopping=False;finishing=False;submitted=False;timeout=False
    task_ids=set();evidence={};evidence_errors=[];capture_threads=[]

    class Bridge(QObject):
        captured=Signal(str,object,object)
    bridge=Bridge()

    def save_evidence(label):
        def capture():
            try:
                if not controller.screen_capture_allowed:raise ValueError('Screenshot permission was revoked.')
                observation=controller.windows.capture(background_only=True)
                if not controller.screen_capture_allowed:raise ValueError('Screenshot permission was revoked.')
                path=Path(observation['path']);digest=hashlib.sha256(path.read_bytes()).hexdigest()
                current=controller.windows.bound_window()
                state={'window':current.to_dict() if current else None,
                       'permission':inspect_game_input_permission(current or {})}
                bridge.captured.emit(label,{**observation,'sha256':digest,'window_state':state},None)
            except Exception as error:bridge.captured.emit(label,None,getattr(error,'code',type(error).__name__))
        thread=threading.Thread(target=capture,daemon=True,name='acceptance-evidence-'+label)
        capture_threads.append(thread);thread.start()

    def gather():
        task=controller.current_task()
        if task:task_ids.add(task['id'])
        tasks=[controller.engine.get_task(identifier) for identifier in task_ids]
        calls=controller.runtime.calls()  # Includes persona routing before a new task ID is assigned.
        report.update(task=task,tasks=tasks,input=budget.snapshot(),runtime_calls=calls,
            usage=usage_summary(calls),model_requests=len(calls),game_inputs=len(budget.snapshot()['submitted']),
            elapsed_seconds=round(time.monotonic()-started,3),evidence=copy.deepcopy(evidence),
            evidence_errors=list(evidence_errors))
        report['task_events']={identifier:controller.engine.events(identifier) for identifier in task_ids}
        report['checkpoints']={identifier:controller.runtime.load(identifier) for identifier in task_ids}
        report['resources_review']['before']=evidence.get('before')
        report['resources_review']['after']=evidence.get('after')

    def progress():
        if finishing:return
        stop_gate.poll()
        try:gather();write_json(output/'progress.json',report)
        except Exception:pass  # Final report remains the authoritative snapshot.

    def finish():
        nonlocal finishing
        if finishing:return
        finishing=True;timer.stop();progress_timer.stop()
        try:
            if not getattr(controller,'_closed',False):gather()
            current=report.get('task')
            report['status']=('timed_out' if timeout else 'stopped_by_request' if report.get('stop_requested') else 'goal_reported_complete_review_required'
                if current and current['status']=='completed' else 'needs_review_or_input')
            report['completed_at']=time.time()
            write_json(output/'acceptance-report.json',report);write_json(output/'progress.json',report)
        finally:close_acceptance(controller,app,window)

    def captured(label,observation,error):
        nonlocal submitted
        if finishing:return
        if observation:evidence[label]=observation
        if error:evidence_errors.append({'phase':label,'code':error})
        if label=='before':
            stop_gate.poll()
            if stopping or report.get('stop_requested') or getattr(controller,'_closed',False):
                # The stop path owns release/final evidence. A closed controller
                # cannot be queried or submitted to after its databases close.
                if not stopping and not finishing:
                    evidence_errors.append({'phase':'after','code':'controller_closed'});finish()
                return
            if error:
                report['errors'].append('Cannot acquire the initial screenshot; input was not started.')
                finish();return
            submitted=submit_initial_goal(controller,goal,stop_requested=bool(report.get('stop_requested')),
                                          stopping=stopping,finishing=finishing)
            if submitted:timer.start()
        else:finish()
    bridge.captured.connect(captured)

    def poll():
        nonlocal stopping
        if submitted and not controller.is_busy and not controller.execution.active and not stopping:
            stopping=True;timer.stop();save_evidence('after')

    def begin():
        # Permissions are explicitly scoped to this input-opted-in invocation.
        if stopping or finishing:return
        try:
            controller.bind_window(metadata['window']['hwnd'])
            current=controller.windows.bound_window()
            if current is None or current.pid!=metadata['window']['pid']:raise ValueError('Game identity changed before start.')
            permission=inspect_game_input_permission(current);report['execution_permission']=permission
            if permission.get('requires_elevation'):raise ValueError('Elevation is required; no UAC is approved by this tool.')
            controller.set_capture_permission(True);controller.set_game_input_permission(True)
            save_evidence('before')
        except Exception as error:
            report['errors'].append(getattr(error,'code',type(error).__name__));finish()

    def request_stop(reason):
        nonlocal timeout,stopping
        if finishing or stopping:return
        timeout=reason=='timeout';stopping=True
        report['stop_requested']={'reason':reason,'at':time.time()}
        timer.stop()
        if not getattr(controller,'_closed',False):controller.stop(silent=True)
        # Wait for production input release before final evidence; no new phase.
        def stopped():
            if finishing:return
            if getattr(controller,'_closed',False):
                evidence_errors.append({'phase':'after','code':'controller_closed'});finish()
            elif not controller.is_busy and not controller.execution.active:save_evidence('after')
            else:QTimer.singleShot(100,stopped)
        stopped()
        QTimer.singleShot(5000,finish)

    def deadline():request_stop('timeout')

    timer=QTimer();timer.setInterval(250);timer.timeout.connect(poll)
    stop_gate=StopFileGate(stop_file,lambda:request_stop('stop_file'))
    progress_timer=QTimer();progress_timer.setInterval(500);progress_timer.timeout.connect(progress);progress_timer.start()
    QTimer.singleShot(round(args.start_delay_seconds*1000),begin)
    QTimer.singleShot(780000,deadline)  # Production job remains bounded to its own 720s budget.
    if window is not None:
        window.pet.present_message('system',f'{args.phase} 验收将在 {args.start_delay_seconds:g} 秒后开始；仅本阶段，最多 {args.input_attempt_cap} 次输入尝试。')
    try:app.exec()
    finally:
        if not finishing:
            try:gather()
            except Exception:pass
            report['status']='interrupted'
            write_json(output/'acceptance-report.json',report)
        controller.close();lock.unlock()
    print(json.dumps({'status':report['status'],'phase':args.phase,'input_attempts':len(budget.attempts),
        'submitted_inputs':len(budget.results),'recorded_model_calls':report.get('model_requests',0),
        'report':str(output/'acceptance-report.json'),'independent_review':'required'},ensure_ascii=True),flush=True)
    return 0 if report['status']=='goal_reported_complete_review_required' else 1


def main(argv=None):
    args=build_parser().parse_args(argv)
    try:return run(args)
    except (ValueError,OSError) as error:
        # Never print arbitrary exception text, a Key path or model payload.
        if sys.stderr:print('Acceptance setup failed: '+type(error).__name__,file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())

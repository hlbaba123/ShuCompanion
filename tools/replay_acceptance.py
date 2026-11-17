"""Recorded-frame acceptance for the production route/coordinator, without OS IO.

Default: offline frame/guard/hitbox self-check, no credential or network access.
Explicit --run-model --key-file: production DeepSeek requests using an isolated
empty profile, but every capture and click remains a Python replay operation.
This tests model/agent logic, not live capture, Windows input or battle ability.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import sys
import threading
import time
from unittest.mock import patch
import uuid


ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from shu.windows import WindowError, WindowInfo


HWND=900001
PID=900002
WIDTH,HEIGHT=1920,1080
GOAL='先进入基建查看再返回主页，不领取奖励不改配置'
HITBOXES={
    'home_base':(.73,.88,.77,.92),
    'home_mission':(.57,.72,.76,.90),
    'back':(0.,.10,0.,.12),
    'confirm_yes':(.55,.75,.60,.75),
}


def _dump(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def _emit(value):
    if sys.stdout:print(json.dumps(value,ensure_ascii=True),flush=True)


def _png_size(path):
    with path.open('rb') as file:header=file.read(24)
    if header[:8]!=b'\x89PNG\r\n\x1a\n' or header[12:16]!=b'IHDR':
        raise ValueError('Replay source is not a PNG: '+str(path))
    return struct.unpack('>II',header[16:24])


def _recordings(report_path):
    report=json.loads(report_path.read_text(encoding='utf-8-sig'))
    events=report.get('events',[])
    observations={}
    for event in events:
        if event.get('type')=='observation':
            details=event.get('details',{})
            if isinstance(details,dict) and details.get('id') and details.get('path'):
                path=Path(details['path'])
                # Support moving the whole workspace after the original run.
                if not path.is_file():path=report_path.parent/'data/captures'/path.name
                observations[details['id']]=path
    return events,observations


def load_frames(source_report,mission_report=None):
    events,observations=_recordings(source_report)
    if not observations:raise ValueError('Recorded report has no local observations')
    frames={'home':next(iter(observations.values()))}
    for event in events:
        if event.get('type')!='verification':continue
        details=event.get('details',{});identifier=details.get('observation_id')
        if identifier not in observations:continue
        evidence=details.get('evidence','')
        if details.get('verification')=='confirmed':
            if '控制中枢' in evidence and 'base' not in frames:
                frames['base']=observations[identifier]
            if '是否确认离开' in evidence and 'confirm' not in frames:
                frames['confirm']=observations[identifier]
            if '主页' in evidence:
                frames['homefinal']=observations[identifier]
        elif details.get('verification')=='unknown' and '过渡' in evidence and 'loading' not in frames:
            frames['loading']=observations[identifier]
    if mission_report is not None and mission_report.is_file():
        mission_events,mission_observations=_recordings(mission_report)
        for event in mission_events:
            details=event.get('details',{})
            identifier=details.get('observation_id')
            if (event.get('type')=='verification' and details.get('verification')=='failed'
                    and '任务界面' in details.get('evidence','') and identifier in mission_observations):
                frames['mission']=mission_observations[identifier];break
    for name in ('home','base','confirm','homefinal'):
        if name not in frames:raise ValueError('Missing labelled replay frame: '+name)
    for name,path in frames.items():
        if not path.is_file() or _png_size(path)!=(WIDTH,HEIGHT):
            raise ValueError('Replay frame missing or wrong geometry: '+name)
    return frames


class ReplayWindows:
    """A file-backed screenshot/finite-state adapter with no native backend."""
    def __init__(self,frames,output,*,include_loading=False):
        self.frames=dict(frames);self.capture_dir=Path(output)/'captures'
        self.capture_dir.mkdir(parents=True,exist_ok=True)
        self.state='home';self.visited=['home'];self.clicks=[];self.captures=[]
        self.include_loading=include_loading and 'loading' in self.frames
        self.input_enabled=False;self._stop=threading.Event();self._bound=False
        self._latest=None;self._latest_at=0.;self._returned_home=False
        self._lock=threading.RLock()
        self.info=WindowInfo(HWND,PID,'明日方舟',0,0,WIDTH,HEIGHT,True,False,
                             'replay/Arknights.exe','RecordedReplayWindow')

    def list_windows(self,game_only=True):return [self.info]
    def bind(self,hwnd):
        if hwnd!=HWND:raise WindowError('不是回放窗口。','replay_wrong_window')
        with self._lock:
            self._bound=True;self._latest=None;self.input_enabled=False
        return self.info
    def bound_window(self):return self.info if self._bound else None
    def focus_for_capture(self):
        if not self._bound:raise WindowError('回放尚未绑定。','window_missing')
        return self.info
    def require_input_permission(self):
        return {'requires_elevation':False,'diagnostic_replay':True,'message':'仅回放内存状态，不发送系统输入。'}
    def discover_capabilities(self):
        return {'windows':False,'window_enumeration':True,'background_capture_available':False,
                'recorded_image_replay':True,'real_game_inputs':0,'real_screen_captures':0}
    def set_stop(self):
        self._stop.set()
        with self._lock:self.input_enabled=False;self._latest=None
    def reset_stop(self):self._stop.clear()
    def set_input_enabled(self,enabled):
        if not isinstance(enabled,bool):raise ValueError('input permission must be bool')
        with self._lock:self.input_enabled=enabled
    def _state(self,state):
        self.state=state
        if not self.visited or self.visited[-1]!=state:self.visited.append(state)

    def capture(self,background_only=True):
        with self._lock:
            if not self._bound:raise WindowError('回放尚未绑定。','window_missing')
            state=self.state
            source=self.frames['homefinal' if state=='home' and self._returned_home else state]
            identifier=uuid.uuid4().hex
            path=self.capture_dir/(identifier+'.png')
            shutil.copyfile(source,path)
            observation={'id':identifier,'captured_at':time.time(),'window_id':HWND,'window_pid':PID,
                'path':str(path.resolve()),'width':WIDTH,'height':HEIGHT,
                'method':'recorded_screenshot_replay','warnings':[],'state':'unknown',
                'client_rect':{'left':0,'top':0,'width':WIDTH,'height':HEIGHT},
                'coordinate_space':'client_physical_pixels','background_capture':True,
                'window_executable':self.info.executable,'window_class':self.info.window_class}
            self._latest=copy.deepcopy(observation);self._latest_at=time.monotonic()
            self.captures.append({'id':identifier,'replay_state':state,'path':str(path.resolve()),
                                  'source':str(source),'real_screen_capture':False})
            # One recorded loading frame, then local polls obtain stable base.
            if state=='loading':self._state('base')
            return observation

    @staticmethod
    def _inside(x,y,name):
        left,right,top,bottom=HITBOXES[name]
        return left<=x<=right and top<=y<=bottom

    def controlled_click(self,x,y,observation_id,cancel_event=None):
        with self._lock:
            if self._stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
                raise WindowError('回放任务已停止。','stopped')
            if not self.input_enabled:raise WindowError('回放输入未授权。','input_disabled')
            if not self._bound:raise WindowError('回放尚未绑定。','window_missing')
            if not self._latest or self._latest['id']!=observation_id:
                raise WindowError('回放观察已过期或已消费。','stale_observation')
            if time.monotonic()-self._latest_at>5:
                raise WindowError('回放观察太旧。','stale_observation')
            if type(x) is not int or type(y) is not int or not (0<=x<WIDTH and 0<=y<HEIGHT):
                raise WindowError('回放点击坐标超出客户区。','out_of_bounds')
            self._latest=None
            nx,ny=x/(WIDTH-1),y/(HEIGHT-1)
            before=self.state;hit=None
            if before=='home' and self._inside(nx,ny,'home_base'):
                hit='home_base';self._state('loading' if self.include_loading else 'base')
            elif before=='home' and 'mission' in self.frames and self._inside(nx,ny,'home_mission'):
                hit='home_mission';self._state('mission')
            elif before=='mission' and self._inside(nx,ny,'back'):
                hit='back';self._state('home')
            elif before=='base' and self._inside(nx,ny,'back'):
                hit='back';self._state('confirm')
            elif before=='confirm' and self._inside(nx,ny,'confirm_yes'):
                hit='confirm_yes';self._returned_home=True;self._state('home')
            result={'ok':True,'action':'click','x':x,'y':y,'window_id':HWND,
                    'observation_id':observation_id,'executed_at':time.time(),
                    'simulated':True,'real_game_input':False,'replay_hit':hit,
                    'state':'unverified','verification_required':True}
            self.clicks.append({**result,'before':before,'after':self.state})
            return result

    def controlled_drag(self,*args,**kwargs):
        raise WindowError('本回放不支持拖拽，也不会发送系统输入。','replay_drag_forbidden')


def self_check(frames,output,include_loading=False):
    replay=ReplayWindows(frames,output/'offline-check',include_loading=include_loading)
    checks=[]
    def reject(label,operation,code):
        try:operation()
        except WindowError as error:
            if error.code!=code:raise AssertionError(label+': wrong rejection') from None
        else:raise AssertionError(label+': unexpectedly accepted')
        checks.append(label)
    def click(nx,ny):
        observation=replay.capture()
        return replay.controlled_click(round(nx*(WIDTH-1)),round(ny*(HEIGHT-1)),observation['id'])
    reject('unbound capture',lambda:replay.capture(),'window_missing')
    replay.bind(HWND);first=replay.capture()
    reject('default input disabled',lambda:replay.controlled_click(100,100,first['id']),'input_disabled')
    replay.reset_stop();replay.set_input_enabled(True)
    reject('wrong observation id',lambda:replay.controlled_click(100,100,'old-id'),'stale_observation')
    reject('out of bounds',lambda:replay.controlled_click(WIDTH,10,first['id']),'out_of_bounds')
    replay._latest_at-=6
    reject('expired frame',lambda:replay.controlled_click(100,100,first['id']),'stale_observation')
    first=replay.capture()
    unknown=replay.controlled_click(WIDTH//2,HEIGHT//2,first['id'])
    assert unknown['replay_hit'] is None and replay.state=='home'
    checks.append('unlabelled click leaves frame unchanged')
    reject('consumed frame',lambda:replay.controlled_click(100,100,first['id']),'stale_observation')
    reject('drag forbidden',lambda:replay.controlled_drag(1,2,3,4,.4,'id'),'replay_drag_forbidden')
    click(.80,.85)
    if replay.state=='loading':replay.capture()
    assert replay.state=='base';replay.capture()
    click(.04,.05);assert replay.state=='confirm'
    click(.67,.67);assert replay.state=='home'
    checks.append('base-confirm-home finite state transitions')
    if 'mission' in frames:
        click(.65,.84);assert replay.state=='mission'
        click(.04,.05);assert replay.state=='home'
        checks.append('mission misclick and back recovery')
    a,b=replay.capture(),replay.capture()
    assert a['id']!=b['id'] and a['path']!=b['path']
    assert a['client_rect']=={'left':0,'top':0,'width':WIDTH,'height':HEIGHT}
    assert Path(a['path']).read_bytes()==frames['homefinal'].read_bytes()
    checks.append('fresh IDs/time/paths preserve recorded pixels and geometry')
    replay.set_stop()
    reject('stop blocks further clicks',lambda:replay.controlled_click(20,20,b['id']),'stopped')
    report={'status':'passed','mode':'offline_replay_self_check','model_calls':0,'credential_reads':0,
            'real_game_inputs':0,'real_screen_captures':0,'checks':checks,
            'annotated_hitboxes':HITBOXES,'frame_sources':{name:str(path) for name,path in frames.items()},
            'source_sha256':{name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in frames.items()},
            'visited':replay.visited,'fake_clicks':len(replay.clicks)}
    _dump(output/'self-check.json',report)
    return report


def run_model(frames,output,key_file,timeout_seconds=180,include_loading=False,segment_steps=12):
    from PySide6.QtCore import QCoreApplication
    from shu.controller import SessionController
    profile=output/'data'
    if profile.exists() and any(profile.iterdir()):
        raise ValueError('Replay model profile must be empty; choose another --output directory')
    if not key_file.is_file():raise ValueError('The explicit --key-file does not exist')
    app=QCoreApplication.instance() or QCoreApplication([])
    replay=ReplayWindows(frames,profile,include_loading=include_loading)
    started=time.monotonic();metrics=[];route_calls=0;game_calls=0;notices=[];errors=[];messages=[];finished=[]
    controller=None
    report={'status':'failed','mode':'production_route_recorded_replay','real_game_inputs':0,
            'real_screen_captures':0,'isolated_empty_profile':True,'key_copied':False,
            'goal':GOAL,'limitations':'Recorded-state replay only; no live game/window/input or tower-defense capability is tested.'}

    def trace_client(client,role):
        operation=client.complete
        def complete(*args,**kwargs):
            nonlocal route_calls,game_calls
            if role=='route':route_calls+=1
            else:game_calls+=1
            try:return operation(*args,**kwargs)
            finally:
                value=copy.deepcopy(getattr(client,'last_metrics',{}))
                metrics.append({'role':role,**value})
        client.complete=complete
        return client

    with ExitStack() as guards:
        # Patch from construction onward. The native backend sentinel makes any
        # accidental regression fail before obtaining a native window handle.
        native=guards.enter_context(patch('shu.windows._NativeWindows',side_effect=AssertionError('Native windows forbidden in replay')))
        guards.enter_context(patch('shu.controller.WindowController',return_value=replay))
        try:
            controller=SessionController(output/'isolated-root',profile,key_path=key_file,legacy_tools=False)
            controller.windows=replay;controller.execution.windows=replay
            controller.execution.wiki=None
            controller.execution.max_seconds=timeout_seconds
            controller.execution.max_decisions=18;controller.execution.max_model_calls=18
            controller.execution.segment_steps=segment_steps
            trace_client(controller.client,'route')
            factory=controller.execution.client_factory
            controller.execution.client_factory=lambda:trace_client(factory(),'game')
            controller.message.connect(lambda role,text:messages.append({'role':role,'text':text}))
            controller.error.connect(errors.append)
            controller.notice.connect(lambda text:notices.append(text))
            controller.execution.finished.connect(lambda value:finished.append(copy.deepcopy(value)))
            controller.set_capture_permission(True)
            controller.set_game_input_permission(True)
            controller.submit(GOAL)
            last_print=0
            while time.monotonic()-started<timeout_seconds:
                app.processEvents();elapsed=time.monotonic()-started
                if elapsed-last_print>=5:
                    task=controller.current_task()
                    _emit({'replay_progress':round(elapsed,1),'task_status':task.get('status') if task else 'routing',
                           'fake_clicks':len(replay.clicks),'route_calls':route_calls,'game_calls':game_calls,
                           'real_game_inputs':0,'real_screen_captures':0})
                    last_print=elapsed
                if not controller.is_busy and not controller.execution.active and elapsed>.5:
                    break
                time.sleep(.03)
            else:
                report['deadline_exceeded']=True;controller.stop(silent=True)
                until=time.monotonic()+2
                while (controller.is_busy or controller.execution.active) and time.monotonic()<until:
                    app.processEvents();time.sleep(.02)
            app.processEvents()
            task=controller.current_task()
            events=controller.engine.events(task['id']) if task else []
            runtime=controller.runtime.load(task['id']) if task else None
            states_ok={'base','confirm'}.issubset(replay.visited) and replay.state=='home'
            completed=bool(task and task.get('status')=='completed')
            passed=completed and states_ok and not errors and not native.called and not report.get('deadline_exceeded')
            report.update(status='passed' if passed else 'needs_input',task=task,
                coordinator_completed=completed,replay_outcome_verified=states_ok,
                visited=replay.visited,final_state=replay.state,fake_clicks=len(replay.clicks),
                route_calls=route_calls,game_calls=game_calls,model_metrics=metrics,
                runtime_metrics=(runtime or {}).get('metrics',{}),execution_results=finished,
                errors=errors,native_backend_constructions=native.call_count,
                elapsed_seconds=round(time.monotonic()-started,3),
                trace_paths={'events':str(output/'events.json'),'captures':str(output/'captures.json'),
                             'clicks':str(output/'clicks.json'),'messages':str(output/'messages.json'),
                             'report':str(output/'replay-report.json')})
            _dump(output/'events.json',events);_dump(output/'captures.json',replay.captures)
            _dump(output/'clicks.json',replay.clicks);_dump(output/'messages.json',messages)
        finally:
            if controller is not None:controller.close()
    _dump(output/'replay-report.json',report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-report',type=Path,default=ROOT/'artifacts/execution-admin-013/execution-report.json')
    parser.add_argument('--mission-report',type=Path,default=ROOT/'artifacts/execution-admin-014/execution-report.json')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/replay-020')
    parser.add_argument('--include-loading',action='store_true',help='Replay one recorded loading frame before base')
    parser.add_argument('--run-model',action='store_true',help='Explicitly allow DeepSeek API calls; still no OS/game input')
    parser.add_argument('--key-file',type=Path,help='Existing local Key text; never copied or printed')
    parser.add_argument('--timeout-seconds',type=int,default=180)
    parser.add_argument('--segment-steps',type=int,choices=range(1,13),default=12)
    args=parser.parse_args(argv)
    if args.run_model and args.key_file is None:parser.error('--run-model requires an explicit --key-file')
    if not 10<=args.timeout_seconds<=180:parser.error('timeout must be 10..180 seconds')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    try:
        frames=load_frames(args.source_report.resolve(),args.mission_report.resolve())
        checked=self_check(frames,output,args.include_loading)
        if not args.run_model:
            _emit({'status':checked['status'],'mode':checked['mode'],'checks':len(checked['checks']),
                   'model_calls':0,'credential_reads':0,'real_game_inputs':0,'real_screen_captures':0,
                   'report':str(output/'self-check.json')})
            return 0
        result=run_model(frames,output,args.key_file.resolve(),args.timeout_seconds,args.include_loading,args.segment_steps)
        _emit({key:result.get(key) for key in ('status','coordinator_completed','replay_outcome_verified',
            'route_calls','game_calls','fake_clicks','elapsed_seconds','real_game_inputs','real_screen_captures','trace_paths')})
        return 0 if result['status']=='passed' else 1
    except Exception as error:
        # Do not persist arbitrary exception text from a network/provider path.
        failure={'status':'failed','error_type':type(error).__name__,'real_game_inputs':0,'real_screen_captures':0}
        _dump(output/'replay-failure.json',failure);_emit(failure)
        return 2


if __name__=='__main__':raise SystemExit(main())

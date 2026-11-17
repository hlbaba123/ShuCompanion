"""One loaded Copilot: observe, navigate, start once, monitor and stop.

No game battle input is exposed. The one supervised desktop worker owns both
surfaces, and every effect crosses the existing SQLite submission gate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from dataclasses import replace

from .core import ActionRejected
from .gui_supervisor import GuiSupervisor
from .maa_battle_vision import BattleVision, BattleVisionError
from .maa_queue_receipt import _publish
from .workflow_store import _encode


class BattleError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _box(value):
    return (isinstance(value,dict) and all(type(value.get(k)) in (int,float)
            and math.isfinite(value[k]) and 0<=value[k]<=1 for k in ('x','y','width','height'))
            and value['width']>0 and value['height']>0
            and value['x']+value['width']<=1 and value['y']+value['height']<=1)


def verified_ready(frame, view, target):
    box = view.get('start_action_box')
    return (view.get('action')=='ready' and view.get('page')=='stage_detail'
            and view.get('selected_stage_code')==target['stage_code']
            and view.get('difficulty')==target['difficulty'] and view.get('resource_sensitive') is False
            and view.get('image_sha256')==frame['image_sha256']
            and view.get('screenshot_id')==frame['screenshot_id'] and _box(box)
            and box['x']>.55 and box['y']>.65)


def loaded_target(view, document, contract):
    stage = view.get('stage_code','').upper()
    if (view.get('page')=='copilot' and view.get('running') is False
            and not any(view.get(k) for k in ('mystery_code','stage_code','preview_title'))):
        raise BattleError('loaded_job_missing','当前MAA尚未加载作业；请先导入单作业，旧缓存不能替代当前预览。')
    title = document.get('doc',{}).get('title','')
    visible=re.sub(r'\s+','',str(view.get('preview_title',''))).rstrip('.…')
    canonical=re.sub(r'\s+','',title)
    if (view.get('page')!='copilot' or view.get('running') is not False
            or view.get('multi_mode') is not False or not _box(view.get('start_box'))
            or not re.fullmatch(r'prts://[1-9][0-9]{0,11}',view.get('mystery_code',''))
            or not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+)+',stage)
            or not document.get('actions') or len(visible)<4 or visible not in canonical
            or not re.search(r'(?<![A-Z0-9-])'+re.escape(stage)+r'(?![A-Z0-9-])', title, re.I)):
        raise BattleError('loaded_job_unverified','当前只支持已加载、关卡一致的单作业；未启动历史多作业队列。')
    requested=(contract.get('task_spec') or {}).get('stage_code')
    if requested and requested.upper()!=stage:
        raise BattleError('loaded_job_wrong_stage','已加载的作业与本次指定关卡不同。')
    difficulty='raid' if re.search(r'突袭',title) and not re.search(r'普通.*突袭|普通.{0,4}兼容|兼容',title) else 'normal'
    return {'stage_code':stage,'difficulty':difficulty,'mystery_code':view['mystery_code'],
            'title':title,'stage_id':document.get('stage_name')}


def _point(box,frame):
    size=frame['window']['client_rect']
    return round((box['x']+box['width']/2)*(size['width']-1)),round((box['y']+box['height']/2)*(size['height']-1))


def navigation_target(target, members, facts):
    """Carry an exact public zone into vision; never infer a chapter from EX/S."""
    invalid=lambda: BattleError('navigation_chapter_unverified','尚未核实目标关卡所属章节，未猜测入口或启动作战。')
    if not members or facts.get('ok') is not True:raise invalid()
    ids={row.get('stage_id') for row in members}
    activities={row.get('event') for row in members}
    if (len(ids)!=1 or len(activities)!=1 or not next(iter(activities))
            or any(row.get('code')!=target['stage_code'] for row in members)
            or not any(target['stage_id'] in (row.get('stage_id'),row.get('level_id')) for row in members)):
        raise invalid()
    groups=[g for g in facts.get('groups',[]) if ids.intersection(g.get('stage_ids',[]))]
    if len(groups)!=1:raise invalid()
    group=groups[0]
    refs=group.get('source_refs')
    if (not group.get('group_id') or group['group_id']=='catalog-unmapped'
            or not isinstance(group.get('label'),str) or not group['label'].strip()
            or not isinstance(refs,list) or not refs
            or any(not isinstance(r,dict) or not r.get('path')
                   or not re.fullmatch(r'[0-9a-f]{64}',str(r.get('sha256',''))) for r in refs)):
        raise invalid()
    return {**target,'activity_name':next(iter(activities)),
            'stage_name':members[0].get('name') or '',
            'chapter_id':group['group_id'],'chapter_name':group['label'],
            'chapter_source_refs':refs,'account_unlock_verified':False}


def _content_hash(value):
    # Box facts may exceed the workflow's bounded JSON envelope; they remain
    # in the facts DB. Only their fingerprint enters the battle receipt.
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
                                    separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()


def verify_execution_integrity(maa,game):
    for report in (maa,game):
        if (not isinstance(report,dict) or report.get('known') is not True
                or report.get('input_allowed') is not True
                or type(report.get('target_integrity')) is not int):
            raise BattleError('execution_integrity_unverified','无法核实黍、MAA与游戏的输入权限，未启动作战。')
    if maa['target_integrity']<game['target_integrity']:
        raise BattleError('maa_game_integrity_mismatch','MAA本身的权限低于游戏，需要以管理员运行MAA后再验收；仅提升黍不够。')


class BattleRunner:
    def __init__(self, roles, context, cancel):
        self.roles,self.store,self.context,self.cancel=roles,roles.store,context,cancel
        self.root=roles.directory/'task-private'/context['task_id']/'maa-battle'/str(context['epoch'])
        self.root.mkdir(parents=True,exist_ok=True)
        self.supervisor=None
        self.start_operation=None
        self.target=None
        self.evidence=[]
        self.stopping=threading.Event()
        self.sequence=0
        self.running_ref=None
        self.stop_result=None
        self.stop_deadline=None
        self.automatic_pause_state=None
        # Explicit current acceptance policy after measured background message
        # delivery produced no game transition. Never silently use Seize.
        self.navigation_mode='background'
        self.navigation_phase='return_home'
        config=roles.clients.get('vision',roles.clients['maa']).config
        self.vision=BattleVision(config,progress=self.progress)
        from .battle_local_navigation import LocalBattleNavigation
        self.local_navigation = LocalBattleNavigation(self.roles.directory/'framework-observation-logs')
        roles.clients['vision']=self.vision.client
        self._stop_lock=threading.Lock()

    def progress(self,name,stage,value):
        self.roles._progress(stage,'maa',self.context,self.cancel,name,value)

    def guard(self):
        if self.stopping.is_set():raise ActionRejected('MAA停止中')
        self.roles._guard(self.context,self.cancel,input_required=True)
        if not self.roles.screen_permission():raise ActionRejected('需要读取MAA和游戏画面的授权')

    def publish(self,kind,payload):
        ref=_publish(self.root,kind,payload)
        from .task_cleanup import TaskCleanup
        cleanup=TaskCleanup(self.store)
        with self.store.engine._transaction():
            lifecycle=self.store.management._lifecycle(self.context['task_id'])['state']
            if lifecycle not in {'active','trashed'}:raise ActionRejected('任务已进入清理')
            path=cleanup._safe_path(self.context['task_id'],ref['path'])
            self.store._db.execute('INSERT OR IGNORE INTO workflow_cleanup_files(task_id,path,sha256) VALUES(?,?,?)',
                (self.context['task_id'],str(path),ref['sha256']))
        self.evidence.append(ref)
        return ref

    def snapshot(self,surface,*,cancel=None,keep_background=False):
        timeout=12
        if self.stop_deadline is not None:
            timeout=min(timeout,self.stop_deadline-time.monotonic())
            if timeout<=0:raise BattleError('stop_timeout','MAA停止核对已到40秒总时限，保留未决状态。')
        response=self.supervisor.request({'op':'snapshot','args':{'surface':surface}},timeout=timeout,
            cancel_event=cancel if cancel is not None else self.cancel)
        if (keep_background and surface == 'maa' and response.get('status') != 'completed'
                and response.get('error') == 'minimized'):
            raise BattleError('monitor_maa_minimized','MAA 已最小化，暂时无法读取后台画面。')
        if (surface=='maa' and response.get('status')!='completed' and response.get('error')=='minimized'
                and not self.stopping.is_set()):
            # MAA may minimize itself when Copilot starts. Capture stays
            # read-only: restoration is the existing admitted focus operation,
            # never a hidden native capture side effect or another Start.
            self.progress('恢复MAA监控窗口','tool_started',
                {'summary':'MAA已最小化，正在通过既有授权恢复后重新截图；不会再次启动作战。'})
            try:self.focus('maa')
            except BattleError as error:
                # A known foreground denial may still have restored the
                # window. Only a new non-minimized background capture can
                # establish that; unknown/partial focus outcomes stay blocked.
                if error.code!='focus_not_acquired':raise
            self.guard()
            response=self.supervisor.request({'op':'snapshot','args':{'surface':'maa'}},timeout=timeout,
                cancel_event=cancel if cancel is not None else self.cancel)
        if self.stopping.is_set() and surface=='maa' and response.get('error')=='minimized':
            raise BattleError('minimized','MAA监控窗口已最小化。')
        if response.get('status')!='completed':
            raise BattleError('capture_unverified','截图未完成：'+str(response.get('error','unknown')))
        return response['result']

    def input(self,args,*,starts=False):
        self.guard();self.sequence+=1
        if starts:self._validate_related_import_pin()
        action={'kind':'start' if starts else 'battle_navigation', 'maa_identity':self.supervisor.target_identity,
                'game_identity':getattr(self,'game_identity',None),**args}
        if starts and self.context.get('import_dispatch_id') is not None:
            pin = self.import_pin
            action.update(import_dispatch_id=self.context['import_dispatch_id'],
                          import_proof_ref={'path':pin.proof_path,'sha256':pin.proof_sha256})
        elif starts and getattr(self,'related_import_binding',False):
            pin=self.import_pin
            action.update(source_import_task_id=pin.source_task_id,
                          import_proof_ref={'path':pin.proof_path,'sha256':pin.proof_sha256},
                          source_import_binding={'task_id':pin.source_task_id,
                              'revision':pin.source_revision,'epoch':pin.source_epoch,
                              'dispatch_id':pin.import_dispatch_id,
                              'receipt_ref':{'path':pin.proof_path,'sha256':pin.proof_sha256}})
        operation=self.store.prepare_operation(self.context['task_id'],self.context['dispatch_id'],
            f"{self.context['dispatch_id']}:battle:{self.sequence}",action,starts=starts)
        operation_id=operation['operation_id'];self.store.issue_operation(operation_id)
        if starts:self.start_operation=operation_id
        def admit():
            self.guard()
            if starts:self._validate_related_import_pin()
            current=self.store.get_operation(operation_id)
            if current.get('submitted_at'):return current
            return self.store.enter_submission(operation_id)
        response=self.supervisor.request({'op':'act','args':args},timeout=10,
            cancel_event=self.cancel,admit_callback=admit)
        submitted=self.store.get_operation(operation_id).get('submitted_at') is not None
        status='verified' if submitted and response.get('status')=='completed' else 'unknown' if submitted else 'not_issued'
        ref=self.publish('native-input',{'operation_id':operation_id,'response':response,'args':args})
        receipt=self.store.finish_operation(operation_id,status,{'evidence_ref':ref,'input_dispatched':status=='verified'})
        if (not starts and args.get('surface')=='game' and args.get('action') in ('click','drag')
                and receipt['status']=='not_issued' and receipt['submitted_at'] is None
                and response.get('status')=='not_issued' and response.get('submission_entered') is False
                and response.get('error')=='game_target_changed'):
            raise BattleError('navigation_target_changed','导航目标在提交前发生变化，本次操作未发送。')
        if status!='verified':raise BattleError('input_unverified','输入结果未确认，未重复输入：'+str(response.get('error','unknown')))
        if args.get('action')=='focus' and response.get('result',{}).get('focus_acquired') is not True:
            raise BattleError('focus_not_acquired','已核实本次窗口激活未取得前台，未继续发送游戏内容输入。')
        return operation_id

    def focus(self,surface):
        self.input({'surface':surface,'action':'focus'})

    def _local(self,frame,*,cancel=None,expected=None):
        single=self.roles._import_vision_readback(self.context,self.cancel)
        observer=getattr(single,'local',single)
        return observer.observe(frame['image_path'],expected=expected,cancel_event=cancel or self.cancel)

    def _single_mode(self,frame):
        local=self._local(frame)
        option=local.get('options',{}).get('multiple_jobs',{})
        if (local.get('page')!='copilot' or local.get('image_sha256')!=frame.get('image_sha256')
                or option.get('state') is not False
                or option.get('measurement')!='local_checkbox_pixels'):
            raise BattleError('single_mode_unverified','未独立核实多作业模式已关闭，未启动历史队列。')

    def _expected_import_pin(self):
        return getattr(self,'import_pin',None) or getattr(self.roles,'battle_expected_job',None)

    def _requires_import_source(self):
        contract=self.context['contract']
        request=contract.get('original_user_request',contract.get('user_request',''))
        # Preserve the old unqualified "run the currently loaded single job"
        # entry. A cross-turn import reference or an explicit difficulty needs
        # real import evidence; a compatible title cannot resolve its mode.
        return bool(re.search(r'(?:刚才|刚刚|上一轮|上轮|上次|前一轮).{0,32}(?:导入|载入|加载)'
                              r'|(?:导入|载入|加载).{0,16}(?:刚才|刚刚|上一轮|上轮|上次|前一轮)'
                              r'|普通|突袭',request))

    def _validate_related_import_pin(self):
        if getattr(self,'related_import_binding',False):
            from .battle_import_binding import load_related_import_binding, ImportBindingError
            if load_related_import_binding(self.store,self.context) != self.import_pin:
                raise ImportBindingError()

    def _import_bound_target(self,target,raw):
        self._validate_related_import_pin()
        pin=self._expected_import_pin()
        if pin is None:return target
        from .battle_import_binding import bind_import_target
        return bind_import_target(pin,target,
            self.supervisor.target_identity,hashlib.sha256(raw).hexdigest())

    def _fresh_target(self,surface,frame,box,*,cancel=None):
        from .game_executor import compare_target_patch
        fresh=self.snapshot(surface,cancel=cancel)
        if frame['window']['client_rect']!=fresh['window']['client_rect']:
            raise BattleError('layout_changed','窗口尺寸变化，请重新观察。')
        size=frame['window']['client_rect']
        before={**frame,'path':frame['image_path'],'width':size['width'],'height':size['height']}
        after={**fresh,'path':fresh['image_path'],'width':size['width'],'height':size['height']}
        action={'kind':'click','x':box['x']+box['width']/2,'y':box['y']+box['height']/2}
        if not compare_target_patch(before,after,action):
            raise BattleError('target_changed','决策后按钮画面已变化，未发送点击。')
        return fresh

    def _loaded_view(self,frame,document):
        view=self.vision.inspect_maa(frame,self.cancel)
        try:
            loaded_target(view,document,self.context['contract'])
            return view
        except BattleError as error:
            if error.code not in ('loaded_job_unverified','loaded_job_missing'):raise
        pin=self._expected_import_pin()
        if pin is None:return view
        expected={'code':pin.mystery_code,'stage_code':pin.stage_code,
                  'title':document.get('doc',{}).get('title',''),'operator_names':[]}
        local=self._local(frame,expected=expected)
        self.publish('loaded-local-check',{'frame':frame,'observed':local,'model_observed':view})
        start=local.get('anchors',{}).get('start',{}).get('rect')
        if (local.get('image_sha256')!=frame['image_sha256'] or local.get('page')!='copilot'
                or local.get('idle') is not True or local.get('running') is not False
                or local.get('single_job') is not True or local.get('expected_title_visible') is not True
                or local.get('expected_stage_visible') is not True
                or local.get('codes')!=[pin.mystery_code] or not isinstance(start,dict)):
            return view
        w=frame['window']['client_rect']['width'];h=frame['window']['client_rect']['height']
        # Text was independently found in this exact PNG by the existing local
        # OCR parser. The import pin still independently binds code and stage.
        return {**view,'page':'copilot','running':False,'multi_mode':False,
                'mystery_code':pin.mystery_code,'stage_code':pin.stage_code,
                'preview_title':expected['title'],'stop_box':None,
                'start_box':{'x':start['x']/w,'y':start['y']/h,
                             'width':start['width']/w,'height':start['height']/h},
                'evidence':'当前图像的本地OCR独立核实完整标题、关卡、代码、单作业和开始按钮。'}

    def _loaded(self):
        from . import maa_load_receipt
        from .roster_match import assess_requirements
        from .maa_facts import RosterStore
        frame=self.snapshot('maa');self._single_mode(frame)
        # These are public Copilot bytes saved by MAA, never game memory or an
        # internal account protocol. Pin them before navigation and before start.
        from .maa_queue_receipt import _read_stable
        raw,state=_read_stable(maa_load_receipt._CACHE_PATH)
        document=json.loads(raw.decode('utf-8-sig'))
        view=self._loaded_view(frame,document)
        self.publish('loaded-observation',{'frame':frame,'observed':view,
                     'cache_sha256':hashlib.sha256(raw).hexdigest()})
        target=loaded_target(view,document,self.context['contract'])
        target=self._import_bound_target(target,raw)
        from .level_catalog import find_levels
        directory=find_levels(target['stage_code'],self.root/'catalog',cache_root=self.roles.directory)
        matches=[row for row in directory.get('levels',[]) if str(row.get('code','')).upper()==target['stage_code']
                 and str(document.get('stage_name','')).casefold() in
                     {str(row.get('stage_id','')).casefold(),str(row.get('level_id','')).casefold()}]
        if not matches:
            raise BattleError('loaded_stage_directory_mismatch','当前作业内部关卡与真实公开目录不一致，未开战。')
        self.publish('loaded-directory',directory)
        from .activity_public_facts import stage_facts
        try:
            facts=stage_facts({'stages':matches},self.roles.directory/'public-facts')
        except (OSError,ValueError) as error:
            raise BattleError('navigation_chapter_unverified','目标关卡的公开章节资料暂不可用，未猜测导航路径。') from error
        self.publish('navigation-chapter-facts',facts)
        target=navigation_target(target,matches,facts)
        self.progress('核实导航章节','tool_finished',{'summary':
            target['activity_name']+' → '+target['chapter_name']+' → '+target['stage_code']+
            ('（突袭）' if target['difficulty']=='raid' else '（普通）')})
        box=RosterStore(self.store).current('local-default')
        assessment=assess_requirements(document,box or {})
        if assessment.get('classification')!='satisfied':
            raise BattleError('loaded_roster_unverified','当前作业所需干员尚未通过本地Box核对，未开战。')
        self.document_hash=hashlib.sha256(raw).hexdigest()
        self.document=document
        self.roster_hash=_content_hash(box)
        self.publish('loaded-job',{'observed':view,'target':target,'document_sha256':self.document_hash,
                                   'cache_state':state,'assessment':assessment})
        return target

    def _navigation_observation(self,history):
        # Format errors and a locally identified home-banner rejection share
        # this one read-only budget. No action has been dispatched on rejection.
        target={**self.target,'navigation_phase':getattr(self,'navigation_phase','return_home')}
        retry_history=list(history)
        for attempt in range(3):
            self.guard()
            frame=self.snapshot('game')
            try:
                local = getattr(self, 'local_navigation', None)
                if local is not None:
                    view = local.observe(frame, target, self.cancel)
                    if view is not None:
                        return frame, view
                view=self.vision.navigate(frame,target,history=retry_history,cancel_event=self.cancel)
                proposal=copy.deepcopy(view)
                proposal_ref=self.publish('model-navigation-proposal',
                    {'frame':frame,'target':target,'attempt':attempt+1,'proposal':proposal})
                if local is not None:
                    view = local.check_model_home_action(frame, view, target)
                self.publish('model-navigation-guard',{'frame':frame,'target':target,
                    'attempt':attempt+1,'proposal_ref':proposal_ref,'guarded':view})
                if (proposal.get('action') in ('click','drag')
                        and proposal.get('resource_sensitive') is False
                        and view.get('page')=='home' and view.get('action')=='ask'
                        and view.get('resource_sensitive') is False
                        and view.get('model_action_rejected') is True
                        and view.get('rejection_code')=='home_banner_target_unverified'
                        and attempt<2):
                    retry_history.append({'page':'home','navigation_phase':target['navigation_phase'],
                        'action_not_submitted':True,'rejection_code':'home_banner_target_unverified',
                        'evidence':'上次首页目标未通过本地核对，未派发点击或拖动。当前已知是首页，'
                                   '仅重新定位首页当前活动的banner；不要选择终端整卡或其他活动，'
                                   '不要把上次被拒绝的proposal当作已执行动作。'})
                    self.progress('重新定位首页活动入口','tool_started',
                        {'summary':'首页活动目标未通过核对，重新截图定位；尚未发送导航输入。',
                         'attempt':attempt+2})
                    if self.cancel.wait(.2):self.guard()
                    continue
                return frame,view
            except BattleVisionError as error:
                if error.code!='battle_vision_invalid_output':raise
                self.publish('vision-invalid-output',{'frame':frame,'attempt':attempt+1,
                    'code':error.code,'diagnostic':error.evidence})
                if attempt==2:raise
                self.progress('重新观察游戏画面','tool_started',
                    {'summary':'本次只读视觉格式未通过，重新截图核对；不重复输入。','attempt':attempt+2})
                if self.cancel.wait(.2):self.guard()

    def _navigate(self):
        # A prior attempt or an already selected detail page cannot substitute
        # for this run's independently observed home checkpoint.
        self.navigation_phase='return_home'
        for name in ('home_ref','ready_ref','ready_frame','ready_view'):
            self.__dict__.pop(name,None)
        self.publish('navigation-phase',{'navigation_phase':self.navigation_phase,'target':self.target})
        self.progress('先返回游戏首页','tool_started',{'navigation_phase':self.navigation_phase,
            'summary':'先识别当前页面并返回首页，核实首页后再进入目标活动。'})
        if getattr(self,'navigation_mode','background')=='foreground':
            try:self.focus('game')
            except BattleError as error:
                if error.code!='focus_not_acquired':raise
                self.progress('等待游戏前台','tool_started',
                    {'summary':'请点击游戏窗口一次并保持前台，黍会自动继续，无需回到聊天。'})
                deadline=time.monotonic()+90
                while True:
                    self.guard()
                    observed=self.snapshot('game')
                    if observed['window'].get('foreground') is True:break
                    if time.monotonic()>=deadline:
                        raise BattleError('game_foreground_timeout','等待游戏前台超时，未点击开始。')
                    if self.cancel.wait(.5):self.guard()
        history=[];previous=None;repeats=0;seen_frames=set()
        target_changes=0
        for number in range(40):
            self.guard()
            frame,view=self._navigation_observation(history)
            self.publish('navigation-observation',{'frame':frame,'view':view,'navigation_phase':self.navigation_phase})
            if (not frame.get('screenshot_id') or not frame.get('image_sha256')
                    or view.get('screenshot_id')!=frame['screenshot_id']
                    or view.get('image_sha256')!=frame['image_sha256']
                    or frame['screenshot_id'] in seen_frames):
                raise BattleError('navigation_frame_unverified','导航观察未绑定本次新截图，未继续操作。')
            seen_frames.add(frame['screenshot_id'])
            self.progress('返回游戏首页' if self.navigation_phase=='return_home' else '导航到 '+self.target['stage_code'],
                'tool_finished',{'summary':view['evidence'],'page':view['page'],'navigation_phase':self.navigation_phase})
            if view.get('resource_sensitive') is not False:
                raise BattleError('navigation_needs_input','当前画面涉及资源或尚未核实，未继续导航。')
            if self.navigation_phase=='return_home':
                if view['page']=='home' and view['action']=='wait':
                    self.home_ref=self.publish('navigation-home',{'frame':frame,'view':view})
                    self.navigation_phase='to_target'
                    self.publish('navigation-phase',{'navigation_phase':self.navigation_phase,
                        'target':self.target,'home_ref':self.home_ref})
                    self.progress('首页已核实，前往目标活动','tool_finished',{'navigation_phase':self.navigation_phase,
                        'summary':'当前截图已确认游戏首页，继续按已核实的活动与章节导航。'})
                    history.append({'page':'home','navigation_phase':'return_home','evidence':view['evidence']})
                    continue
                if view['action']=='ready' or view['page']=='home' and view['action'] in ('click','drag'):
                    raise BattleError('navigation_home_unverified','本轮尚未确认游戏首页，未接受目标关卡就绪或活动入口操作。')
            if self.navigation_phase=='to_target' and verified_ready(frame,view,self.target):
                # A second independent fresh observation prevents transition
                # frames and arbitrary map labels from becoming a start permit.
                if self.cancel.wait(.2):self.guard()
                fresh,again=self._navigation_observation(history)
                self.publish('navigation-observation',{'frame':fresh,'view':again,'navigation_phase':self.navigation_phase})
                if not fresh.get('screenshot_id') or fresh['screenshot_id'] in seen_frames:
                    raise BattleError('navigation_frame_unverified','关卡复核没有取得独立新截图，未启动MAA。')
                seen_frames.add(fresh['screenshot_id'])
                if verified_ready(fresh,again,self.target):
                    self.ready_frame,self.ready_view=fresh,again
                    self.ready_ref=self.publish('game-ready',{'target':self.target,'home_ref':self.home_ref,
                        'navigation_phase':self.navigation_phase,'frames':[frame,fresh],'observations':[view,again]})
                    return
                continue
            if view['action']=='ask':raise BattleError('navigation_needs_input',view['evidence'])
            if view['action']=='wait':
                if self.cancel.wait(1):self.guard()
                continue
            if view['action']=='ready':
                raise BattleError('navigation_not_ready','当前关卡详情未通过目标及图像核对，未继续操作。')
            key=(view['page'],view['target_label'],view['action'])
            if key==previous and repeats>=3:raise BattleError('navigation_no_progress','连续导航未获得新页面证据，已停止盲目重试。')
            size=frame['window']['client_rect'];x,y=_point(view['target_box'],frame)
            regions=[]
            if _box(view.get('start_action_box')):
                b=view['start_action_box'];regions.append({'kind':'start_action','rect':{
                    'x':b['x']*size['width'],'y':b['y']*size['height'],
                    'width':b['width']*size['width'],'height':b['height']*size['height']}})
            args={'surface':'game','action':view['action'],'screenshot_id':frame['screenshot_id'],
                  'target':view['target_label'],'x':x,'y':y,'forbidden_regions':regions}
            if view['action']=='drag':args.update(to_x=round(view['to_x']*(size['width']-1)),
                to_y=round(view['to_y']*(size['height']-1)),duration_seconds=.5)
            try:
                self.input(args)
            except BattleError as error:
                if error.code!='navigation_target_changed':
                    raise
                target_changes+=1
                if target_changes>2:
                    raise BattleError('navigation_target_unstable','连续三次截图中的导航目标在点击或拖动前发生变化，三次操作都未发送。') from error
                self.progress('重新核对导航目标','tool_started',{'navigation_phase':self.navigation_phase,
                    'summary':'目标仍在变化，本次操作未发送，等待画面稳定后重新定位。','attempt':target_changes})
                if self.cancel.wait(.5 * target_changes):self.guard()
                continue
            target_changes=0
            repeats=repeats+1 if key==previous else 1
            previous=key
            history.append({'page':view['page'],'target':view['target_label'],'evidence':view['evidence'],
                            'navigation_phase':self.navigation_phase})
            if self.cancel.wait(.4):self.guard()
        raise BattleError('navigation_limit','本轮导航达到40步上限，未启动MAA。')

    def _prestart_loaded(self):
        from .maa_load_receipt import _CACHE_PATH
        from .maa_queue_receipt import _read_stable
        for attempt in range(3):
            self.guard()
            frame=self.snapshot('maa');self._single_mode(frame)
            view=self._loaded_view(frame,self.document)
            raw,_=_read_stable(_CACHE_PATH)
            self.publish('prestart-loaded-observation',{'frame':frame,'observed':view,
                'attempt':attempt+1,'cache_sha256':hashlib.sha256(raw).hexdigest()})
            try:
                target=loaded_target(view,self.document,self.context['contract'])
            except BattleError as error:
                # A rejected visual transcription is read-only, not an input
                # outcome. Reobserve without replaying navigation or Start.
                if error.code!='loaded_job_unverified' or attempt==2:raise
                if self.cancel.wait(.2):self.guard()
                continue
            return frame,view,self._import_bound_target(target,raw),raw

    def _state(self,frame,*,independent_cancel=None):
        # Native local OCR is the economical monitor; the visual model is used
        # only when layout/text recognition cannot establish state.
        if independent_cancel is not None and self.stop_deadline is not None:
            remaining=self.stop_deadline-time.monotonic()
            if remaining<=0:raise BattleError('stop_timeout','MAA停止核对超时，未重复操作。')
            self.stop_vision.client.config.timeout_seconds=min(12,remaining)
            value=self.stop_vision.inspect_maa(frame,independent_cancel)
            return {**value,'idle':value['page']=='copilot' and not value['running'] and value['start_box'] is not None}
        local=self._local(frame,cancel=independent_cancel)
        if (local.get('ok') is True and local.get('page') == 'copilot'
                and local.get('image_sha256') == frame.get('image_sha256')
                and type(local.get('idle')) is bool and type(local.get('running')) is bool
                and local['idle'] != local['running']):
            return {'page':'copilot','running':local.get('running') is True,
                    'idle':local.get('idle') is True,'local':True,
                    'start_box':None,'stop_box':None}
        value=self.vision.inspect_maa_state(frame,independent_cancel or self.cancel)
        return {**value,'idle':value['page']=='copilot' and value['running'] is False and value['start_box'] is not None}

    def _monitor_observation(self):
        for attempt in range(3):
            self.guard()
            try:
                frame = self.snapshot('maa', keep_background=True)
            except BattleError as error:
                if error.code != 'monitor_maa_minimized' or attempt == 2:
                    raise
                self.cancel.wait(1)
                continue
            try:
                state = self._state(frame)
            except BattleVisionError as error:
                if error.code != 'battle_vision_invalid_output':
                    raise
                diagnostic = {key: error.evidence[key] for key in ('validation','expected_tool')
                              if key in error.evidence}
                self.publish('monitor-invalid-output', {'frame': frame, 'attempt': attempt + 1,
                    'code': error.code, 'diagnostic': diagnostic})
                if attempt == 2:
                    raise
                self.progress('重新读取MAA运行状态', 'tool_started',
                    {'summary': '监控识别结果未通过校验，正在获取新截图。', 'attempt': attempt + 2})
                self.cancel.wait(.2)
                continue
            state = {**state, 'reobservation_attempts': attempt}
            return frame, state, self._observation(frame, state)

    def _observation(self,frame,state):
        size=frame['window']['client_rect']
        return self.publish('battle-observation',{'schema':'maa_battle_observation/v1',
            **{k:self.context[k] for k in ('task_id','revision','epoch')},
            'start_operation_id':self.start_operation,'maa_identity':self.supervisor.target_identity,
            'captured_at':frame.get('captured_at',time.time()),'screenshot_id':frame['screenshot_id'],
            'running':state['running'],'idle':state['idle'],'width':size['width'],'height':size['height'],
            'screenshot_ref':{'path':frame['image_path'],'sha256':frame['image_sha256']},
            'image_sha256':frame['image_sha256'],'method':frame['method']})

    def _settle(self,idles):
        operation=self.store.get_operation(self.start_operation)
        proof=self.publish('battle-idle',{'schema':'maa_battle_idle/v1',
            **{k:self.context[k] for k in ('task_id','revision','epoch')},
            'start_operation_id':self.start_operation,'start_payload_hash':operation['payload_hash'],
            'battle_start_submitted':True,'external_idle_confirmed':True,
            'maa_identity':self.supervisor.target_identity,'observed_running_ref':self.running_ref,
            'idle_observation_refs':idles,'control_seq':self.store.current(self.context['task_id'])['control_seq']})
        self.store.settle_battle_idle(self.start_operation,proof)
        return proof

    def run(self):
        from .battle_contract import authorized, compound_authorized
        from .maa_visual_native import _NativeMaaBackend
        from .battle_windows import ensure_game_window, ensure_maa_window
        try:
            self.guard()
            if not authorized(self.context['contract']):raise BattleError('battle_permission_missing','本次没有单次MAA开战授权。')
            try:
                self.store.require_start_attempt_available(self.context['task_id'])
            except ActionRejected as error:
                raise BattleError('battle_attempt_unavailable', str(error)) from error
            if compound_authorized(self.context['contract']):
                from .battle_import_binding import load_current_import_binding, ImportBindingError
                dispatch = self.store.get_dispatch(self.context['dispatch_id'])
                import_dispatch_id = self.context.get('import_dispatch_id')
                if (self.context.get('execution_step') != 'battle' or not isinstance(import_dispatch_id,str)
                        or dispatch['status'] != 'running' or dispatch['role'] != 'maa'
                        or any(dispatch[key] != self.context[key] for key in ('task_id','revision','epoch'))
                        or any(dispatch['payload'].get(key) != self.context[key]
                               for key in ('contract','execution_step','import_dispatch_id'))):
                    raise ImportBindingError()
                self.import_pin = load_current_import_binding(self.store, self.context['task_id'], import_dispatch_id)
                self.guard()
            elif (self.context['contract'].get('source_task_id')
                    or self._expected_import_pin() is None and self._requires_import_source()):
                from .battle_import_binding import load_related_import_binding
                self.import_pin = load_related_import_binding(self.store,self.context)
                self.related_import_binding = True
                self.guard()
            # An explicit acceptance report may already supply a verified pin;
            # it is a separate declared source, never a scan for a latest job.
            if self.store.pending_operations():raise BattleError('prior_input_unresolved','已有未决输入，先核对再开始。')
            maa = ensure_maa_window(self.guard)
            game = ensure_game_window(self.guard)
            native=_NativeMaaBackend()
            self.game_identity={'hwnd':game.hwnd,'pid':game.pid,'executable':game.executable,
                                'creation_time':native.creation_time(game.pid)}
            identity={k:maa[k] for k in ('hwnd','pid','creation_time','executable')}
            permissions={'maa':native.permission(identity['pid']),'game':native.permission(game.pid)}
            self.publish('execution-permissions',permissions)
            verify_execution_integrity(**permissions)
            with self.roles._supervisor_lock:
                if self.roles.supervisor is not None:self.roles.supervisor.close()
                self.supervisor=GuiSupervisor(identity,backend='battle',navigation_identity=self.game_identity,
                    navigation_mode=getattr(self,'navigation_mode','background'),directory=self.roles.directory)
                self.roles.supervisor=self.supervisor
            prior=getattr(self.roles,'focus_recovery_profile',None)
            if prior is not None:
                from .maa_focus_recovery import reconcile_known_focus_failure,process_identity_retired
                recovery=reconcile_known_focus_failure(self.store,self.supervisor,
                    context=self.context,guard=self.guard,prior_profile=prior,
                    expected_worker_state_sha256=getattr(self.roles,'focus_recovery_state_sha256',None),
                    retired_identity=lambda old:process_identity_retired(old,native_backend=native),
                    game_identity=self.game_identity,cancel_event=self.cancel)
                self.evidence.extend(recovery.get('evidence_refs',[]))
                if recovery['status']=='blocked':
                    raise BattleError(recovery['code'],'旧窗口激活失败的证据未能核实，保留输入屏障。')
            # _loaded() captures MAA through snapshot(), including its bounded
            # minimized-window recovery. Do not preempt that recovery with an
            # unconditional foreground requirement from discovery metadata.
            if getattr(game, 'minimized', False):
                self.input({'surface': 'game', 'action': 'restore'})
            from .maa_battle_settings import verify_single_run
            self.single_run_settings=verify_single_run(identity)
            self.publish('single-run-settings',self.single_run_settings)
            self.target=self._loaded()
            self.progress('当前作业已核对','tool_finished',{'summary':self.target['stage_code']+' '+self.target['mystery_code']})
            self._navigate()
            self.guard()
            # Game navigation remains background. MAA's WPF Start button did
            # not react to a successful PostMessage dispatch in the real run.
            # Reuse import's guarded activation, then reobserve the loaded job
            # before authorizing a single foreground Start click.
            self.focus('maa')
            self.guard()
            frame,view,current_target,raw=self._prestart_loaded()
            from .maa_facts import RosterStore
            if (any(current_target[k]!=self.target[k] for k in current_target)
                    or hashlib.sha256(raw).hexdigest()!=self.document_hash
                    or verify_single_run(identity)!=self.single_run_settings
                    or _content_hash(RosterStore(self.store).current('local-default'))!=self.roster_hash):
                raise BattleError('loaded_job_changed','导航期间MAA作业或本地Box已变化，未启动。')
            # MAA vision may take seconds. Confirm the game detail pane has
            # not changed in the meantime before consuming the start permit.
            current_game=self.snapshot('game')
            from .game_executor import compare_target_patch
            before={**self.ready_frame,'path':self.ready_frame['image_path']}
            after={**current_game,'path':current_game['image_path']}
            if self.ready_frame['window']!=current_game['window']:
                # Foreground legitimately changes when focusing MAA; geometry
                # and the process identity must still be exactly pinned.
                for key in ('hwnd','pid','creation_time','executable','client_rect'):
                    if self.ready_frame['window'][key]!=current_game['window'][key]:
                        raise BattleError('game_ready_changed','开战前游戏身份或几何已变化。')
            for point in ((.82,.15),(.8,.29),(.86,.89)):
                if not compare_target_patch(before,after,{'kind':'click','x':point[0],'y':point[1]}):
                    raise BattleError('game_ready_changed','开战前游戏关卡详情画面已变化，未启动MAA。')
            fresh=self._fresh_target('maa',frame,view['start_box']);x,y=_point(view['start_box'],fresh)
            self.input({'surface':'maa','action':'click','target':'battle_start','x':x,'y':y,
                        'screenshot_id':fresh['screenshot_id'],'ready_ref':self.ready_ref},starts=True)
            self.progress('MAA已提交单次启动，正在核对运行','tool_finished',{})
            idles=[];started=time.monotonic()
            while time.monotonic()-started<600:
                _,state,ref=self._monitor_observation()
                if state['reobservation_attempts'] or not state['idle']:
                    idles.clear()
                if state['running']:
                    self.running_ref=self.running_ref or ref;idles=[]
                elif self.running_ref and state['idle']:
                    idles.append(ref)
                    if len(idles)>=2:
                        proof=self._settle(idles[-2:])
                        return {'status':'battle_finished','code':'maa_run_finished',
                            'summary':'已确认MAA运行后恢复空闲；未将该状态等同于关卡胜利。',
                            'battle_start_submitted':True,'external_idle_confirmed':True,
                            'start_operation_id':self.start_operation,'battle_receipt_ref':proof,'evidence_refs':self.evidence[-48:]}
                if not self.running_ref and time.monotonic()-started>45:
                    raise BattleError('maa_start_unverified','MAA启动未获得运行证据，不会重复点击开始。')
                if self.cancel.wait(2):self.guard()
            raise BattleError('maa_monitor_timeout','运行超过10分钟，已请求停止；请查看停止回执。')
        except Exception as error:
            # A recognition/model failure after start must not leave a running
            # MAA without a stop attempt. Cancellation's coordinator stop path
            # is independent and uses fresh observations.
            if self.start_operation and not self.stopping.is_set():
                try:self.stop_result=self.stop(self.context['task_id'],timeout_stop=True)
                except Exception as stop_error:
                    self.stop_result={'external_stop_confirmed':False,'summary':str(stop_error)[:240]}
            return {'status':'needs_input','code':getattr(error,'code','battle_execution_error'),
                'summary':str(error)[:600],'battle_start_submitted':bool(self.start_operation and
                    self.store.get_operation(self.start_operation).get('submitted_at')),
                'external_idle_confirmed':bool((self.stop_result or {}).get('external_stop_confirmed')),
                'stop_result':self.stop_result,'start_operation_id':self.start_operation,
                'automatic_pause_state':getattr(self,'automatic_pause_state',None),'evidence_refs':self.evidence[-48:]}

    def _restore_for_stop(self,task_id,current):
        focus=self.store.prepare_control_operation(task_id,current['control_seq'],
            f"{task_id}:battle-stop-focus:{current['control_seq']}",{'kind':'stop','phase':'focus',
                'maa_identity':self.supervisor.target_identity})
        self.store.issue_operation(focus['operation_id'])
        def admit_focus():
            now=self.store.current(task_id)
            if now['writes_enabled'] or now['control_seq']!=current['control_seq']:raise ActionRejected('停止授权变化')
            existing=self.store.get_operation(focus['operation_id'])
            return existing if existing.get('submitted_at') else self.store.enter_submission(focus['operation_id'])
        focused=self.supervisor.request_stop({'op':'act','args':{'surface':'maa','action':'focus',
            'target':'battle_stop_focus'}},admit_callback=admit_focus,timeout=min(10,max(.1,self.stop_deadline-time.monotonic())))
        entered=self.store.get_operation(focus['operation_id']).get('submitted_at') is not None
        # request_stop deliberately relabels completed control dispatches
        # as "stopping". This is a focus receipt, not external-stop proof;
        # the fresh running/idle observations below remain mandatory.
        result=focused.get('result',{})
        known_focus=(result.get('focus_acquired') is True or
                     (result.get('ok') is True and result.get('action')=='focus'
                      and result.get('focus_acquired') is False))
        # Windows may deny activation after successfully restoring the client.
        # A completed, explicit no-focus result permits only the next fresh
        # background capture. It is not proof that the window was restored or
        # MAA stopped; unknown/partial dispatches still cannot proceed.
        good=(entered and focused.get('status') in {'completed','stopping'} and known_focus)
        self.store.finish_operation(focus['operation_id'],'verified' if good else 'unknown' if entered else 'not_issued',
                                    {'phase':'stop_focus','response':focused})
        return good

    def stop(self,task_id,*,control_seq=None,timeout_stop=False):
        """Independent stop observation is allowed after ordinary input revocation."""
        with self._stop_lock:
            if self.stop_result is not None:return self.stop_result
            self.stopping.set()
            self.stop_deadline=time.monotonic()+40
            if not self.supervisor:return {'external_stop_confirmed':False,'summary':'尚未绑定MAA'}
            self.supervisor.request_stop()
            # A start ID is allocated before the helper crosses its durable
            # submission gate. Preparation/issuance alone grants no authority
            # to stop an external MAA run which this task did not start.
            if (not self.start_operation or
                    self.store.get_operation(self.start_operation).get('submitted_at') is None):
                return {'external_stop_confirmed':False,'summary':'已撤销导航输入；本轮尚未提交MAA启动。'}
            # The coordinator has already durably revoked the task. A watchdog
            # does the same before attempting the stop-only input.
            if timeout_stop:
                paused = self.store.control(task_id, 'pause', expected_epoch=self.context['epoch'])
                self.automatic_pause_state = {
                    **{key: paused[key] for key in ('task_id','revision','epoch','control_seq')},
                    'from_epoch': self.context['epoch'], 'from_revision':self.context['revision'],
                    'start_operation_id':self.start_operation, 'dispatch_id':self.context['dispatch_id']}
            current=self.store.current(task_id);cancel=threading.Event()
            focus_attempted=False
            try:
                frame=self.snapshot('maa',cancel=cancel)
            except BattleError as error:
                if error.code!='minimized':
                    raise
                focus_attempted=True
                if not self._restore_for_stop(task_id,current):
                    return {'external_stop_confirmed':False,'summary':'MAA停止窗口未能恢复，停止状态待核对。'}
                frame=self.snapshot('maa',cancel=cancel)
            stop_vision=BattleVision(replace(self.vision.client.config,timeout_seconds=12,max_tokens=1400))
            self.stop_vision=stop_vision
            view=stop_vision.inspect_maa(frame,cancel)
            if view['running']:
                self.running_ref=self.running_ref or self._observation(frame,{**view,'idle':False})
            # One automatic activation at most. A denied activation still
            # permits read-only idle reconciliation, never a background Stop.
            if view['running'] and frame['window'].get('foreground') is not True:
                if not focus_attempted:
                    if not self._restore_for_stop(task_id,current):
                        return {'external_stop_confirmed':False,'summary':'MAA停止窗口未能激活，停止状态待核对。'}
                    frame=self.snapshot('maa',cancel=cancel)
                    view=stop_vision.inspect_maa(frame,cancel)
                if view['running'] and frame['window'].get('foreground') is not True:
                    return {'external_stop_confirmed':False,'summary':'MAA仍未激活，未派发无效的后台停止消息。'}
            if not view['running']:
                if view.get('page')=='copilot' and view.get('start_box') and self.running_ref:
                    first=self._observation(frame,{**view,'idle':True})
                    time.sleep(.2)
                    fresh=self.snapshot('maa',cancel=cancel);state=self._state(fresh,independent_cancel=cancel)
                    if state['idle']:
                        last=self._observation(fresh,state);self._settle([first,last])
                        self.stop_result={'external_stop_confirmed':True,'summary':'停止核对时MAA已自然结束；连续两次空闲已结清原启动。'}
                        return self.stop_result
                return {'external_stop_confirmed':False,'summary':'停止时MAA已非运行；原作战结果仍待核验。'}
            self.running_ref=self.running_ref or self._observation(frame,{**view,'idle':False})
            box=view['stop_box']
            if not _box(box):return {'external_stop_confirmed':False,'summary':'未找到可核实的MAA停止按钮。'}
            fresh=self._fresh_target('maa',frame,box,cancel=cancel)
            x,y=_point(box,fresh)
            op=self.store.prepare_control_operation(task_id,current['control_seq'],
                f"{task_id}:battle-stop:{current['control_seq']}",{'kind':'stop','maa_identity':self.supervisor.target_identity})
            self.store.issue_operation(op['operation_id'])
            def admit():
                now=self.store.current(task_id)
                if now['writes_enabled'] or now['control_seq']!=current['control_seq']:raise ActionRejected('停止授权变化')
                existing=self.store.get_operation(op['operation_id'])
                return existing if existing.get('submitted_at') else self.store.enter_submission(op['operation_id'])
            response=self.supervisor.request_stop({'op':'act','args':{'surface':'maa','action':'click',
                'target':'battle_stop','screenshot_id':fresh['screenshot_id'],'x':x,'y':y}},admit_callback=admit,timeout=min(10,max(.1,self.stop_deadline-time.monotonic())))
            sent=self.store.get_operation(op['operation_id']).get('submitted_at') is not None
            self.store.finish_operation(op['operation_id'],'unknown' if sent else 'not_issued',{'response':response})
            idles=[]
            for _ in range(8):
                frame=self.snapshot('maa',cancel=cancel);state=self._state(frame,independent_cancel=cancel)
                ref=self._observation(frame,state)
                if state['running']:
                    self.running_ref=self.running_ref or ref;idles=[]
                elif state['idle']:idles.append(ref)
                if len(idles)>=2:
                    if self.running_ref:self._settle(idles[-2:])
                    self.store.finish_operation(op['operation_id'],'verified',{'stopped':True,
                        'evidence_ref':idles[-1],'start_effect_reconciled':bool(self.running_ref)})
                    self.stop_result={'external_stop_confirmed':True,'summary':'MAA停止后已连续两次回读空闲。',
                        'operation_id':op['operation_id'],'evidence_refs':idles}
                    return self.stop_result
                time.sleep(.3)
            self.stop_result={'external_stop_confirmed':False,'summary':'已提交MAA停止，仍需运行状态回读。','operation_id':op['operation_id']}
            return self.stop_result

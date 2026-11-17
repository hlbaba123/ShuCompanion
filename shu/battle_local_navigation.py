"""Recognize common current-event navigation steps in an existing game screenshot."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys
import unicodedata

from .maa_framework_observer import MaaFrameworkOcr, MaaFrameworkOcrError
from .maa_battle_vision import _navigation
from .maa_visual_native import MAA_EXECUTABLE


def _text(value):
    return re.sub(r'[\s，,！!。·]+', '', unicodedata.normalize('NFKC', value)).upper()


def _snapshot_words(frame, ocr):
    rect = frame['window']['client_rect']
    width, height = rect['width'], rect['height']
    if ((width, height) != (ocr['width'], ocr['height']) or
            frame['image_sha256'] != ocr['image_sha256']):
        return []
    words = []
    for row in ocr['words']:
        r = row['rect']
        box = {k: r[k] / (width if k in ('x', 'width') else height) for k in r}
        words.append((_text(row['text']), box))
    return words


def _unique(words, label, *, region=(0, 0, 1, 1)):
    left, top, right, bottom = region
    hits = [box for text, box in words if text == _text(label)
            and left <= box['x'] + box['width']/2 <= right
            and top <= box['y'] + box['height']/2 <= bottom]
    return hits[0] if len(hits) == 1 else None


def _home(words):
    # On the dashboard, squad and operator cards sit below the terminal on
    # the right. The shortcut menu contains the same words in other positions.
    return all(_unique(words,label,region=region) is not None for label,region in (
        ('终端',(.50,.08,.95,.42)),('编队',(.45,.36,.78,.68)),('干员',(.68,.36,.99,.68))))


def _quick_menu(words):
    # The shortcut ribbon and the real home screen share labels. Only the
    # ribbon has these two horizontal rows above the underlying game page.
    upper = ('编队','终端','基建')
    lower = ('干员','档案','公开招募','干员寻访','采购中心')
    return (sum(_unique(words,label,region=(.12,.075,.63,.215)) is not None for label in upper)>=2
            and sum(_unique(words,label,region=(.18,.27,.99,.385)) is not None for label in lower)>=2)


def _quick_home_icon(words,templates):
    label=_unique(words,'首页',region=(0,.15,.20,.45))
    icon=templates.get('quick_home')
    if not label or not icon:
        return None
    cx=icon['x']+icon['width']/2
    cy=icon['y']+icon['height']/2
    # The MAA QuickSwitch/Home icon sits directly below its own caption.
    if label['x']<=cx<=label['x']+label['width'] and label['y']+label['height']/2 < cy <= .45:
        return icon
    return None


def _settings(words):
    # A generic "设置" word alone is not page identity. Check the visible
    # header, reset control and distinct side-menu entries before returning.
    menu=sum(_unique(words,label,region=(0,.15,.21,.95)) is not None
             for label in ('游戏','声音','提醒','按键','用户中心'))
    return (_unique(words,'设置',region=(.10,0,.35,.14)) is not None
            and _unique(words,'重置设置',region=(.7,0,1,.14)) is not None and menu>=2)


def interpret(frame, target, ocr, templates):
    words = _snapshot_words(frame, ocr)
    if not words:
        return None
    labels = {s for s, _ in words}

    returning=target.get('navigation_phase')=='return_home'

    def step(page, label, box, reason, *, action=None):
        value = dict(action=action or ('click' if box else 'ask'), page=page, target_label=label if box else '',
                     target_box=box, to_x=None, to_y=None, selected_stage_code='', difficulty='unknown',
                     start_action_box=None, resource_sensitive=False, evidence=reason)
        return {**_navigation(value, target), 'screenshot_id':frame['screenshot_id'],
                'image_sha256':frame['image_sha256'], 'recognition_engine':'maafw_ocr/maa_templates'}

    if any(any(token in s for token in ('是否使用', '恢复理智', '验证码', '确认购买', '重新登录', '是否退出')) for s in labels):
        return None
    if _quick_menu(words):
        icon=_quick_home_icon(words,templates)
        return step('quick_menu','首页',icon,
                    '快捷菜单双排入口、左侧首页标签及对应图标已核对。' if icon else
                    '快捷菜单覆盖当前页面，尚未核对唯一首页标签及对应图标。')
    if _settings(words):
        back=templates.get('back')
        return step('settings','返回',back,'设置标题、重置设置、侧栏菜单及左上返回模板均已核对。') if back else None
    if ('乐章收录' in labels and (any('STORYLINE' in s for s in labels) or '曲谱' in labels)):
        back = templates.get('back')
        if returning and not back:return None
        reason = ('曲谱目录与左上角返回箭头均已核对，返回上一级。' if back else
                  '当前为曲谱目录，未定位返回按钮。')
        return step('menu','返回',back,reason)
    if {'清除缓存','网络检测'} <= labels and templates.get('start'):
        return step('start','START',templates['start'],'已核对启动页面文字与 START 图标。')
    if {'账号管理','查看公告'} <= labels:
        wake = _unique(words, '开始唤醒', region=(.3,.45,.7,.82))
        if wake and templates.get('wake'):
            return step('wake_up','开始唤醒',wake,'已核对唤醒按钮和登录页模板。')
    if _home(words):
        if returning:
            return step('home','',None,'当前截图已核对首页的终端、干员和编队入口。',action='wait')
        label = target['activity_name']
        banner = _unique(words,label,region=(.80,.12,1,.44))
        if banner:
            return step('home',label,banner,'首页活动入口显示 '+label)
        return None
    # Other recognized menus/maps can be interpreted by vision, but must not
    # enter an event, chapter or stage before the runner observes a fresh home.
    if returning:return None
    chapter = target['chapter_name']
    if '本章回想' in labels and any('作战结束时间' in s for s in labels):
        box = _unique(words,chapter)
        if box and not any('后开启' in s and abs(r['x']-box['x'])<.14
                           and abs(r['y']-box['y'])<.11 for s,r in words):
            return step('event_overview',chapter,box,'活动总览中识别到章节 '+chapter)
        return None
    # MAA ClickStageName uses a full ASCII match on the map. Cross-check with
    # another label from that stage family and leave detail/difficulty to vision.
    stage = target['stage_code'].upper()
    family = stage.split('-')[0]
    codes = {s for s in labels if re.fullmatch(re.escape(family)+r'(?:-[A-Z0-9]+)+',s)}
    if (len(codes)>=2 and stage in codes and not
            any(t in s for s in labels for t in ('开始行动','推荐平均等级','敌方情报','演习','报酬'))):
        box = _unique(words, stage)
        if box:
            return step('stage_map',stage,box,'关卡地图可见多个编号，已定位完整编号 '+stage)
    return None


class LocalBattleNavigation:
    def __init__(self, log_directory):
        project = Path(__file__).resolve().parents[1]
        bundle = Path(getattr(sys, '_MEIPASS', project))
        runtime = bundle/'optional-runtime/5.15.0' if getattr(sys, 'frozen', False) else project.parent/'.maafw-runtime/5.15.0'
        self.ocr = MaaFrameworkOcr(runtime,bundle/'assets/maa-framework',log_directory,timeout=15)
        self.templates = Path(MAA_EXECUTABLE).parent/'resource/template'
        self.home_frame_id = None
        self.quick_menu_frame = None

    def _buttons(self, path, digest, labels):
        import cv2
        import numpy as np
        if '首页' in labels:
            # MAA v6.22 QuickSwitch@ToHome@Entry uses this 1280x720 ROI.
            name, relative, roi = 'quick_home', 'QuickSwitch/Home.png', (11,206,175,355)
        elif '乐章收录' in labels or {'设置','重置设置'} <= labels:
            name, relative, roi = 'back', 'ReturnButton/Return.png', (0,0,220,110)
        elif '清除缓存' in labels:
            name, relative, roi = 'start', 'WakeUp/GameStart.png', (430,550,850,720)
        elif '账号管理' in labels:
            name, relative, roi = 'wake', 'WakeUp/StartToWakeUp.png', (400,340,880,640)
        else:
            return {}
        source = self.templates/relative
        if not source.is_file():
            return {}
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            return {}
        image = cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_GRAYSCALE)
        ref = cv2.imdecode(np.fromfile(str(source),np.uint8),cv2.IMREAD_GRAYSCALE)
        if image is None or ref is None:
            return {}
        view = cv2.resize(image,(1280,720),interpolation=cv2.INTER_AREA)
        x,y,right,bottom = roi
        h,w = ref.shape
        if w>right-x or h>bottom-y:
            return {}
        _,score,_,(dx,dy) = cv2.minMaxLoc(cv2.matchTemplate(view[y:bottom,x:right],ref,cv2.TM_CCOEFF_NORMED))
        if score < .90:
            return {}
        return {name:dict(x=(x+dx)/1280,y=(y+dy)/720,width=w/1280,height=h/720)}

    def observe(self,frame,target,cancel):
        self.home_frame_id = None
        self.quick_menu_frame = None
        if not all(target.get(k) for k in ('activity_name','chapter_name','stage_code','chapter_source_refs')):
            return None
        try:
            path = frame['image_path']
            ocr = self.ocr.recognize(path,cancel_event=cancel)
            words = _snapshot_words(frame,ocr)
            quick_menu = _quick_menu(words)
            if not quick_menu and _home(words):
                self.home_frame_id = frame['screenshot_id']
            buttons = self._buttons(path,frame['image_sha256'],{s for s,_ in words})
            view = interpret(frame,target,ocr,buttons)
            if quick_menu:
                icon=view['target_box'] if view and view['page']=='quick_menu' else None
                self.quick_menu_frame=(frame['screenshot_id'],frame['image_sha256'],icon)
            return view
        except MaaFrameworkOcrError as error:
            if error.code == 'framework_cancelled':
                raise
        except (ImportError,OSError):
            pass
        return None

    def check_model_home_action(self, frame, view, target):
        identity=(frame['screenshot_id'], frame['image_sha256'])
        menu=self.quick_menu_frame
        same_menu=menu is not None and menu[:2]==identity
        if same_menu or view['page']=='quick_menu':
            if view['page']=='quick_menu' and view['action'] in ('ask','wait'):
                return view
            if same_menu and menu[2] and view['page']=='quick_menu' and view['action']=='click' and view['target_label']=='首页':
                icon=menu[2]
                box=view['target_box']
                cx,cy=box['x']+box['width']/2,box['y']+box['height']/2
                if icon['x']<=cx<=icon['x']+icon['width'] and icon['y']<=cy<=icon['y']+icon['height']:
                    return {**view,'target_box':icon}
            return {**view,'page':'quick_menu','action':'ask','target_label':'','target_box':None,
                    'to_x':None,'to_y':None,'evidence':'本张截图未核对模型选择的快捷菜单首页入口。'}
        if self.home_frame_id != frame['screenshot_id'] or view['action'] not in ('click','drag'):
            return view
        # Only the live event banner can leave a verified home screen on this
        # route. An obscuring popup is separately classified by the observer.
        box = view['target_box']
        if (view['page']=='home' and view['action']=='click' and .80 <= box['x']+box['width']/2 <= 1
                and .12 <= box['y']+box['height']/2 <= .44
                and _text(target['activity_name']) in _text(view['target_label'])):
            return view
        return {**view,'page':'home','action':'ask','target_label':'','target_box':None,'to_x':None,'to_y':None,
                'model_action_rejected':True,'rejection_code':'home_banner_target_unverified',
                'evidence':'首页观察未定位右侧最新活动入口，已停止进入其他栏目。'}

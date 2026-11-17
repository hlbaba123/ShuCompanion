"""Model/image protocol and bounded navigation validation; zero native input."""
import copy
import base64
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from shu.providers import ProviderCancelled, ProviderConfig, ProviderError


def png(width=320,height=180):
    def chunk(kind,value):
        return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))
            +chunk(b'IDAT',zlib.compress((b'\x00'+b'\xff\xff\xff'*width)*height))+chunk(b'IEND',b''))


def reply(name,arguments):
    return {'role':'assistant','content':None,'reasoning_content':'PRIVATE REASONING',
            'tool_calls':[{'id':'vision-call','type':'function',
                'function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}}]}


class FakeClient:
    def __init__(self,config):
        self.config=config;self.calls=[];self.responses=[];self.after=None
        self.last_finish_reason='tool_calls'

    def complete(self,messages,**kwargs):
        self.calls.append((copy.deepcopy(messages),copy.deepcopy({k:v for k,v in kwargs.items() if k!='cancel_event'})))
        if self.after:self.after()
        return copy.deepcopy(self.responses.pop(0))


def maa():
    return {'page':'copilot','running':False,'multi_mode':False,'mystery_code':'prts://123',
        'stage_code':'FAKE-1','preview_title':'可见预览标题','operator_names':['测试干员'],
        'start_box':{'x':.2,'y':.2,'width':.1,'height':.1},'stop_box':None,
        'evidence':'自动战斗页中实际可见的预览和按钮。'}


def navigation():
    return {'action':'click','page':'home','target_label':'活动入口',
        'target_box':{'x':.7,'y':.3,'width':.2,'height':.1},'to_x':None,'to_y':None,
        'selected_stage_code':'','difficulty':'unknown','start_action_box':None,
        'resource_sensitive':False,'evidence':'主页右侧可见活动入口。'}


class BattleVisionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.image=Path(temporary.name)/'native.png';self.image.write_bytes(png())
        self.frame={'image_path':str(self.image),'image_sha256':hashlib.sha256(self.image.read_bytes()).hexdigest(),
            'screenshot_id':'current-image','window':{'hwnd':7,'pid':11,'creation_time':13,
                'client_rect':{'x':20,'y':30,'width':320,'height':180}}}
        from shu import maa_battle_vision
        self.module=maa_battle_vision
        self.patch=patch.object(maa_battle_vision,'DeepSeekClient',FakeClient);self.patch.start();self.addCleanup(self.patch.stop)
        self.progress=[]
        self.vision=maa_battle_vision.BattleVision(ProviderConfig(thinking=True),
            progress=lambda *args:self.progress.append(args))

    def inspect(self,value=None):
        self.vision.client.responses=[reply('inspect_maa_battle',maa() if value is None else value)]
        return self.vision.inspect_maa(self.frame)

    def navigate(self,value=None,**kwargs):
        self.vision.client.responses=[reply('navigate_game',navigation() if value is None else value)]
        return self.vision.navigate(self.frame,{'stage_code':'FAKE-1','difficulty':'normal'},**kwargs)

    def test_inspection_is_one_real_game_session_image_call_bound_to_frame(self):
        result=self.inspect()
        self.assertEqual(result['stage_code'],'FAKE-1')
        self.assertEqual(result['image_sha256'],self.frame['image_sha256'])
        self.assertEqual(result['screenshot_id'],'current-image')
        self.assertNotIn('PRIVATE',json.dumps(result))
        messages,kwargs=self.vision.client.calls[0]
        self.assertEqual(len(self.vision.client.calls),1)
        self.assertEqual(kwargs['tools'][0]['function']['name'],'inspect_maa_battle')
        self.assertNotEqual(kwargs.get('tool_choice'),'required')
        self.assertTrue(any(b.get('type')=='image_url' for b in messages[-1]['content']))
        text=messages[-1]['content'][0]['text']
        self.assertIn('320',text);self.assertIn('180',text)
        self.assertTrue(self.progress)

    def test_navigation_click_is_an_observation_not_native_input(self):
        result=self.navigate()
        self.assertEqual(result['action'],'click')
        self.assertEqual(result['target_box'],navigation()['target_box'])
        self.assertEqual(self.vision.client.calls[0][1]['tools'][0]['function']['name'],'navigate_game')

    def test_settings_and_noncombat_menu_only_allow_visible_top_left_return(self):
        back={**navigation(),'page':'settings','target_label':'返回',
              'target_box':{'x':.02,'y':.03,'width':.09,'height':.05}}
        for page in ('settings','menu'):
            self.assertEqual(self.navigate({**back,'page':page})['action'],'click')
            for changes in ({'target_label':'重置设置'}, {'target_label':'游戏'},
                            {'target_box':{'x':.8,'y':.03,'width':.1,'height':.05}},
                            {'target_box':{'x':.1,'y':.1,'width':.3,'height':.3}},
                            {'action':'drag','to_x':.1,'to_y':.1}):
                with self.subTest(page=page,changes=changes),self.assertRaises(ProviderError):
                    self.navigate({**back,'page':page,**changes})

    def test_return_home_phase_prevents_event_selection_until_fresh_home(self):
        target={'stage_code':'FAKE-1','difficulty':'normal','navigation_phase':'return_home'}
        back={**navigation(),'target_label':'返回',
              'target_box':{'x':.02,'y':.03,'width':.09,'height':.05}}
        for page in ('menu','settings','event_overview','stage_map','stage_detail'):
            self.assertEqual(self.module._navigation({**back,'page':page},target)['action'],'click')
            with self.subTest(page=page),self.assertRaises(ProviderError):
                self.module._navigation({**navigation(),'page':page},target)
        for page in ('unknown','settlement','login_required','home'):
            with self.subTest(page=page),self.assertRaises(ProviderError):
                self.module._navigation({**back,'page':page},target)
        home={**navigation(),'action':'wait','target_label':'','target_box':None}
        self.assertEqual(self.module._navigation(home,target)['page'],'home')
        ready={**home,'action':'ready','page':'stage_detail','selected_stage_code':'FAKE-1',
               'difficulty':'normal','start_action_box':{'x':.8,'y':.8,'width':.15,'height':.15}}
        with self.assertRaises(ProviderError):self.module._navigation(ready,target)
        for page,label in (('start','START'),('wake_up','开始唤醒')):
            self.assertEqual(self.module._navigation({**navigation(),'page':page,'target_label':label},target)['action'],'click')

    def test_return_home_accepts_visible_home_icon_beside_back_but_not_right_side(self):
        target={'navigation_phase':'return_home'}
        value={**navigation(),'page':'stage_detail','target_label':'首页',
               'target_box':{'x':.18,'y':.02,'width':.10,'height':.08}}
        self.assertEqual(self.module._navigation(value,target)['target_label'],'首页')
        for changes in ({'target_label':'返回'},
                        {'target_box':{'x':.7,'y':.02,'width':.1,'height':.08}},
                        {'target_box':{'x':.18,'y':.2,'width':.1,'height':.08}}):
            with self.subTest(changes=changes),self.assertRaises(ProviderError):
                self.module._navigation({**value,**changes},target)

    def test_quick_menu_has_separate_home_destination(self):
        selection={**navigation(),'page':'quick_menu','target_label':'首页',
                   'target_box':{'x':.062,'y':.32,'width':.043,'height':.045}}
        for phase in ('return_home','to_target'):
            target={'navigation_phase':phase}
            self.assertEqual(self.module._navigation(selection,target)['target_label'],'首页')
            for change in ({'target_label':'基建'}, {'action':'drag','to_x':.1,'to_y':.4},
                           {'target_box':{'x':.18,'y':.32,'width':.05,'height':.05}},
                           {'target_box':{'x':.06,'y':.02,'width':.04,'height':.06}},
                           {'resource_sensitive':True}):
                with self.subTest(phase=phase,change=change),self.assertRaises(ProviderError):
                    self.module._navigation({**selection,**change},target)

    def test_visual_client_disables_thinking_without_changing_caller_config(self):
        config=ProviderConfig(thinking=True,max_tokens=8192)
        vision=self.module.BattleVision(config)
        self.assertFalse(vision.client.config.thinking)
        self.assertTrue(config.thinking)

    def test_invalid_response_keeps_frame_and_bounded_tool_diagnostic_without_reasoning(self):
        raw=reply('wrong_tool',navigation())
        self.vision.client.responses=[raw]
        self.vision.client.last_finish_reason='length'
        with self.assertRaises(self.module.BattleVisionError) as caught:
            self.vision.navigate(self.frame,{'stage_code':'FAKE-1'})
        evidence=caught.exception.evidence
        self.assertEqual(evidence['screenshot_id'],self.frame['screenshot_id'])
        self.assertEqual(evidence['image_sha256'],self.frame['image_sha256'])
        self.assertEqual(evidence['finish_reason'],'length')
        self.assertEqual(evidence['tool_calls'][0]['name'],'wrong_tool')
        self.assertEqual(json.loads(evidence['tool_calls'][0]['arguments']),navigation())
        self.assertNotIn('PRIVATE',json.dumps(evidence))

    def test_ready_requires_actual_selected_stage_and_visible_start_action(self):
        value={**navigation(),'action':'ready','page':'stage_detail','target_label':'',
            'target_box':None,'selected_stage_code':'FAKE-1','difficulty':'normal',
            'start_action_box':{'x':.8,'y':.8,'width':.15,'height':.15}}
        self.assertEqual(self.navigate(value)['action'],'ready')
        for change in ({'page':'stage_map'},{'selected_stage_code':''},{'start_action_box':None},
                       {'difficulty':'unknown'},{'selected_stage_code':'OTHER-1'}):
            with self.subTest(change=change),self.assertRaises(ProviderError):self.navigate({**value,**change})

    def test_ready_for_wrong_difficulty_is_rejected_as_readonly_invalid_output(self):
        value={**navigation(),'action':'ready','page':'stage_detail','target_label':'',
               'target_box':None,'selected_stage_code':'FAKE-1','difficulty':'normal',
               'start_action_box':{'x':.8,'y':.8,'width':.15,'height':.15}}
        self.vision.client.responses=[reply('navigate_game',value)]
        with self.assertRaises(self.module.BattleVisionError) as caught:
            self.vision.navigate(self.frame,{'stage_code':'FAKE-1','difficulty':'raid'})
        self.assertEqual(caught.exception.code,'battle_vision_invalid_output')

    def test_wrong_tool_multiple_tools_missing_tool_and_truncated_finish_rejected(self):
        valid=reply('inspect_maa_battle',maa())
        for value in (reply('click',maa()),{**valid,'tool_calls':valid['tool_calls']*2},
                      {'role':'assistant','content':'看起来好了'},{**valid,'role':'user'}):
            self.vision.client.responses=[value]
            with self.subTest(value=value),self.assertRaises(ProviderError):self.vision.inspect_maa(self.frame)
        self.vision.client.last_finish_reason='length'
        with self.assertRaises(ProviderError):self.inspect()

    def test_json_duplicates_nonfinite_and_malformed_rejected(self):
        for arguments in ('{bad}',json.dumps(maa()).replace('false','NaN',1),
                          json.dumps(maa())[:-1]+',"page":"copilot"}'):
            value=reply('inspect_maa_battle',maa());value['tool_calls'][0]['function']['arguments']=arguments
            self.vision.client.responses=[value]
            with self.subTest(arguments=arguments),self.assertRaises(ProviderError):self.vision.inspect_maa(self.frame)

    def test_box_out_of_bounds_nonfinite_zero_and_boolean_are_rejected(self):
        for box in ({'x':.95,'y':0,'width':.1,'height':.1},{'x':0,'y':0,'width':0,'height':.1},
                    {'x':True,'y':0,'width':.1,'height':.1},{'x':float('inf'),'y':0,'width':.1,'height':.1}):
            with self.subTest(box=box),self.assertRaises(ProviderError):self.inspect({**maa(),'start_box':box})

    def test_unknown_fields_and_wrong_types_are_rejected(self):
        for changes in ({'running':1},{'multi_mode':'false'},{'operator_names':['重复','重复']},
                        {'preview_title':None},{'extra':'unsafe'},{'mystery_code':'prts://foo'}):
            with self.subTest(changes=changes),self.assertRaises(ProviderError):self.inspect({**maa(),**changes})

    def test_capture_hash_and_geometry_must_match_before_model_call(self):
        for change in ({'image_sha256':'0'*64},{'screenshot_id':''},
                       {'window':{'client_rect':{'width':640,'height':360}}}):
            with self.subTest(change=change),self.assertRaises(ProviderError):
                self.vision.inspect_maa({**self.frame,**change})
        self.assertFalse(self.vision.client.calls)

    def test_verified_capture_bytes_do_not_change_between_hash_check_and_api_encoding(self):
        from PySide6.QtGui import QImage,QColor
        original=self.vision.session.decide
        def mutate_then_decide(*args,**kwargs):
            image=QImage(320,180,QImage.Format.Format_RGB32);image.fill(QColor('black'))
            self.assertTrue(image.save(str(self.image),'PNG'))
            return original(*args,**kwargs)
        with patch.object(self.vision.session,'decide',mutate_then_decide):self.inspect()
        messages=self.vision.client.calls[0][0]
        url=next(b['image_url']['url'] for b in messages[-1]['content'] if b['type']=='image_url')
        transmitted=QImage.fromData(base64.b64decode(url.partition(',')[2]))
        self.assertGreater(transmitted.pixelColor(0,0).red(),240)

    def test_start_action_password_purchase_and_resource_clicks_are_forbidden(self):
        for change in ({'target_label':'开始行动'},{'target_label':'使用源石补充理智'},
                       {'target_label':'请输入验证码'},{'target_label':'寻访十次'},
                       {'resource_sensitive':True},{'page':'login_required'}):
            with self.subTest(change=change),self.assertRaises(ProviderError):self.navigate({**navigation(),**change})
        allowed={**navigation(),'action':'ask','page':'login_required','target_box':None,
                 'target_label':'请用户完成登录','resource_sensitive':True}
        self.assertEqual(self.navigate(allowed)['action'],'ask')

    def test_click_cannot_overlap_observed_start_action(self):
        value={**navigation(),'page':'stage_detail','selected_stage_code':'FAKE-1',
            'difficulty':'normal','target_label':'关卡详情',
            'start_action_box':navigation()['target_box']}
        with self.assertRaises(ProviderError):self.navigate(value)

    def test_drag_needs_bounded_end_point_and_non_drag_has_no_end_point(self):
        value={**navigation(),'action':'drag','page':'stage_map','to_x':.2,'to_y':.4}
        self.assertEqual(self.navigate(value)['to_x'],.2)
        for changes in ({'to_x':None},{'to_y':1.1},{'to_x':False}):
            with self.subTest(changes=changes),self.assertRaises(ProviderError):self.navigate({**value,**changes})
        with self.assertRaises(ProviderError):self.navigate({**navigation(),'to_x':.2})

    def test_game_start_and_wake_are_allowed_but_wait_cannot_hide_a_click(self):
        self.assertEqual(self.navigate({**navigation(),'page':'start','target_label':'START'})['action'],'click')
        self.assertEqual(self.navigate({**navigation(),'page':'wake_up','target_label':'开始唤醒'})['action'],'click')
        with self.assertRaises(ProviderError):self.navigate({**navigation(),'action':'wait'})

    def test_history_is_bounded_data_and_prior_tool_responses_do_not_cross_tasks(self):
        self.inspect()
        self.navigate(history=[{'task_id':'same-task','step':'可见步骤'+str(i),'detail':'x'*1200} for i in range(30)])
        messages=self.vision.client.calls[-1][0]
        text=json.dumps(messages,ensure_ascii=False)
        self.assertNotIn('可见预览标题',text)
        self.assertNotIn('可见步骤0"',text)
        self.assertIn('可见步骤29',text)
        self.assertLess(len(text),26000)

    def test_cancel_before_or_after_response_never_yields_late_observation(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaises(ProviderCancelled):self.vision.inspect_maa(self.frame,cancel_event=cancel)
        self.assertFalse(self.vision.client.calls)
        cancel.clear();self.vision.client.responses=[reply('navigate_game',navigation())]
        self.vision.client.after=cancel.set
        with self.assertRaises(ProviderCancelled):
            self.vision.navigate(self.frame,{'stage_code':'FAKE-1'},cancel_event=cancel)


if __name__=='__main__':unittest.main()

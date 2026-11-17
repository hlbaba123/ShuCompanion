"""Natural intent integration with real contract/store and zero external IO."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication
from shu.controller import SessionController
from shu.providers import ProviderConfig

APP = QCoreApplication.instance() or QCoreApplication([])

class Model:
    def __init__(self, *responses):
        self.responses=list(responses); self.requests=[]; self.config=ProviderConfig(); self.last_metrics={}
    def complete(self,messages,**options):
        self.requests.append({'messages':copy.deepcopy(messages),**options})
        return self.responses.pop(0)

def route(text, goal, intent='execute', **values):
    payload={'intent':intent,'user_request':text,'goal':goal,'constraints':[],
        'completion_criteria':[],'removed_constraints':[],'reply':'我会按完整要求先核对条件。'}
    payload.update(values)
    return {'role':'assistant','content':None,'tool_calls':[{'id':'route', 'type':'function',
        'function':{'name':'route_user_request','arguments':json.dumps(payload,ensure_ascii=False)}}]}

class LeaderControllerTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict('os.environ',{'DEEPSEEK_API_KEY':''}); self.environment.start()
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.c=SessionController(self.root,self.root/'data')
        self.c.key_path.write_text('sk-leader-offline-fixture')
        self.received=[]; self.errors=[]; self.messages=[]
        self.c.error.connect(self.errors.append)
        self.c.message.connect(lambda role,text:self.messages.append((role,text)))
        def submit(goal,contract,*,modify=False,request_cancel=None):
            state=self.c.execution.store.submit(goal,contract,
                task_id=self.c.execution.current_task_id if modify else None)
            self.c.execution.current_task_id=state['task_id']
            self.received.append(copy.deepcopy(state['contract']))
            return {'accepted':True,'task_id':state['task_id'],'game_input_sent':False,'battle_start_submitted':False}
        self.c.execution.submit=submit
    def tearDown(self):
        self.c.close(); self.temp.cleanup(); self.environment.stop()
    def send(self,text,*responses):
        self.c.client=self.c.intent_client=Model(*responses); self.c.persona=self.c._new_persona()
        self.c.submit(text)
        end=time.monotonic()+3
        while self.c.is_busy and time.monotonic()<end:
            APP.processEvents(); time.sleep(.004)
        APP.processEvents(); self.assertFalse(self.c.is_busy)

    def test_import_options_reach_coordinator_through_normal_chat(self):
        text = '导入 A-1 普通作业，开启自动编队，关闭借助战'
        options = {'auto_formation': True, 'use_support': False}
        self.send(text, route(text, text, relation='new', current_deliverable='maa_import',
            target_stage_codes=['A-1'], task_spec={'kind': 'import', 'stage_code': 'A-1'},
            copilot_options=options))
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]['copilot_options'], options)

    def test_full_repeat_goal_reaches_coordinator_and_reply_is_not_fixed_import_pipeline(self):
        text='TL-10刷六次，不使用理智药，也不花源石'
        self.send(text,route(text,'TL-10刷六次',constraints=['不使用理智药','不花源石']))
        self.assertEqual(self.errors,[])
        self.assertEqual(self.received[0]['task_spec']['stage_code'],'TL-10')
        self.assertEqual(self.received[0]['task_spec']['repeat_count'],6)
        self.assertEqual(self.received[0]['task_spec']['medicine_limit'],0)
        self.assertTrue(any('TL-10' in msg and '六次' in msg for role,msg in self.messages if role=='assistant'))
        self.assertFalse(any('正在查找作业并准备导入' in msg for role,msg in self.messages if role=='assistant'))

    def test_context_separates_requested_goal_from_available_capabilities(self):
        summary=json.loads(self.c._task_summary())
        self.assertEqual(summary['assistant_role'],'Arknights MAA supervisor')
        self.assertTrue(summary['capabilities']['planning'])
        self.assertTrue(summary['capabilities']['public_research'])
        self.assertFalse(summary['capabilities']['battle_start'])
        self.assertFalse(summary['capabilities']['maa_gui_import'])
        self.assertNotEqual(summary['mode'],'MAA 作业查询与导入')

    def test_normal_entry_does_not_silently_accept_a_plain_unrouted_execution_reply(self):
        self.send('现在开始刷六次',{'role':'assistant','content':'已接受，马上开始。'})
        self.assertEqual(self.received,[])
        self.assertTrue(self.errors)
        self.assertFalse(any(role=='assistant' and '已接受' in text for role,text in self.messages))

    def test_plain_unrouted_reply_can_only_be_repaired_by_a_valid_structured_route(self):
        text='查询 TL-10 作业并导入'
        self.send(text,{'role':'assistant','content':'好的，我会去找作业。'},
                  route(text,'查询TL-10作业并导入'))
        self.assertEqual(self.errors,[])
        self.assertEqual(len(self.received),1)
        self.assertEqual(self.received[0]['task_spec']['stage_code'],'TL-10')
        self.assertEqual(sum(x['role']=='user' for x in self.c.persona.messages),1)
        self.assertEqual([x['function']['name'] for x in self.c.client.requests[-1]['tools']],['route_user_request'])

    def test_direct_stage_swap_is_rejected_without_any_coordinator_submission(self):
        text='TL-10刷六次，不用药'
        invalid=route(text,'TL-6刷六次',task_spec={'kind':'repeat_stage','stage_code':'TL-6','repeat_count':6})
        self.send(text,invalid,invalid,invalid)
        self.assertEqual(self.received,[])
        self.assertTrue(self.errors)
        self.assertIn('关卡代码',self.errors[-1])

    def test_contract_repair_only_offers_route_and_reuses_actual_source_context(self):
        text='查询当前活动资料并核实来源'
        malformed=route(text,'查询当前活动资料并核实来源',task_spec={'kind':'search'})
        fixed=route(text,'查询当前活动资料并核实来源',task_spec={'kind':'lookup'})
        class Research:
            calls=[]
            def __init__(self,*_args,**_kwargs):pass
            def search(self,query):
                self.calls.append(query)
                return {'ok':True,'data':{'results':[{'url':'https://prts.plus/?op=1','title':'合成只读证据'}]}}
        lookup={'role':'assistant','content':None,'tool_calls':[{'id':'lookup','type':'function',
            'function':{'name':'web_search','arguments':json.dumps({'query':'合成查询'})}}]}
        with patch('shu.controller.ResearchTools',Research):self.send(text,lookup,malformed,fixed)
        self.assertEqual(self.errors,[])
        self.assertEqual(len(self.received),1)
        self.assertEqual(Research.calls,['合成查询'])
        request=self.c.client.requests[-1]
        self.assertEqual([x['function']['name'] for x in request['tools']],['route_user_request'])
        self.assertEqual(request['tool_choice'],{'type':'function','function':{'name':'route_user_request'}})
        context='\n'.join(x.get('content') or '' for x in request['messages'] if x['role']=='system')
        self.assertIn(text,context)
        self.assertIn('任务类别无效',context)
        self.assertTrue(any(x['role']=='tool' and '合成只读证据' in x['content'] for x in request['messages']))
        self.assertEqual(self.c.last_network_report['sources'][0]['url'],'https://prts.plus/?op=1')

    def test_semantic_repair_can_use_second_bounded_attempt_without_repeating_request(self):
        text='查询 TL-10 作业并导入'
        invalid=route('查询TL-6作业','查询TL-6作业并导入')
        invalid2=route(text,'查询TL-10作业并导入',task_spec={'kind':'import','stage_code':'TL-6'})
        self.send(text,invalid,invalid2,route(text,'查询TL-10作业并导入'))
        self.assertEqual(self.errors,[])
        self.assertEqual(len(self.received),1)
        self.assertEqual(self.received[0]['task_spec']['stage_code'],'TL-10')
        self.assertEqual(sum(x['role']=='user' for x in self.c.persona.messages),1)

    def test_lookup_chat_leaves_existing_task_untouched(self):
        question='查一下当前活动，先不要操作MAA'
        self.send(question,route(question,'',intent='chat',reply='我先核对活动资料，不操作MAA。'))
        self.assertEqual(self.received,[])
        self.assertEqual(self.errors,[])

    def test_public_lookup_misclassified_as_observe_is_repaired_without_capture(self):
        text='只读查询TL-10的作业JSON和神秘代码，不导入MAA'
        with patch.object(self.c.windows,'capture',side_effect=AssertionError('Public research must not capture')) as capture:
            self.send(text,route(text,text,intent='observe',task_spec={'kind':'lookup','stage_code':'TL-10'}),
                route(text,text,intent='execute',task_spec={'kind':'lookup','stage_code':'TL-10'}))
        self.assertEqual(self.errors,[])
        self.assertEqual(len(self.received),1)
        self.assertEqual(self.received[0]['intent'],'execute')
        self.assertEqual(self.received[0]['task_spec']['kind'],'lookup')
        self.assertEqual([x['function']['name'] for x in self.c.client.requests[-1]['tools']],['route_user_request'])
        capture.assert_not_called()

if __name__=='__main__': unittest.main()

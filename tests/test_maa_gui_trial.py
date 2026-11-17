from pathlib import Path
import copy, hashlib, importlib.util, json, tempfile, threading, unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('maa_gui_trial',ROOT/'tools/maa_gui_trial.py')
trial=importlib.util.module_from_spec(spec);spec.loader.exec_module(trial)

def action(kind='click', **updates):
    return dict(dict(kind=kind,surface='maa',x=.5,y=.5,to_x=0,to_y=0,
                     text='',seconds=0,purpose='navigate',summary='test'),**updates)

def response(*actions):
    return {'role':'assistant','content':None,'reasoning_content':'retained fake reasoning',
            'tool_calls':[{'id':'call-'+str(i),'type':'function','function':
                          {'name':'shu_gui_step','arguments':json.dumps(a)}} for i,a in enumerate(actions)]}

class FakeClient:
    def __init__(self,responses):
        self.responses=iter(responses);self.requests=[];self.last_metrics={}
        self.config=SimpleNamespace(game_history_turns=6)
    def complete(self,messages,**kwargs):
        self.requests.append(copy.deepcopy(messages))
        return copy.deepcopy(next(self.responses))

class RunnerReliability(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.runner=trial.Trial.__new__(trial.Trial)
        self.runner.out=Path(self.temp.name);self.runner.cancel=threading.Event()
        self.runner.adapters={};self.runner.started=False;self.runner.input_count=0
        self.runner.records=[];self.runner.calls=[];self.runner.allowed_texts={'TL-2','1'}
        self.runner.research_history=[];self.runner.research_facts=[]
        self.runner.knowledge=SimpleNamespace(retrieve=lambda *a,**kw:[])
        self.runner.observe=Mock(side_effect=lambda surface,**kw:{'id':'fake','surface':surface})

    def run_responses(self,responses,max_steps=20,mode='prepare'):
        self.runner.client=FakeClient(responses)
        return self.runner.run({'mode':mode,'goal':'test bounded GUI runner','max_steps':max_steps})

    def test_every_multi_call_receives_rejection_and_prompt_is_retained(self):
        report=self.run_responses([response(action(),action()),response(action('ask'))])
        messages=self.runner.client.requests[1]
        results=[m for m in messages if m['role']=='tool']
        self.assertEqual([m['tool_call_id'] for m in results],['call-0','call-1'])
        self.assertTrue(all(json.loads(m['content'])['executed'] is False for m in results))
        self.assertIn('黍自身工具',messages[0]['content'])
        self.assertIn('retained fake reasoning',str(messages))
        self.assertEqual(report['status'],'needs_input')

    def test_format_failures_stop_on_third_with_explicit_report(self):
        report=self.run_responses([response(action(),action())]*20)
        self.assertEqual(len(self.runner.client.requests),3)
        self.assertEqual(report['status'],'model_format_exhausted')
        self.assertEqual(report['format_failures'],3)
        self.assertEqual(len([r for r in self.runner.records if r['kind']=='model_format_error']),3)

    def test_window_rejections_are_logged_and_same_failure_stops(self):
        self.runner.execute_action=Mock(side_effect=trial.WindowError('fake foreground failure','focus_denied'))
        report=self.run_responses([response(action())]*20)
        errors=[r for r in self.runner.records if r['kind']=='action_error']
        self.assertEqual(report['status'],'no_progress')
        self.assertEqual(len(errors),3)
        self.assertTrue(all(r['code']=='focus_denied' for r in errors))
        self.assertEqual(report['inputs'],0)

    def test_unknown_dispatch_outcome_never_automatically_retries(self):
        for code,details in [('uia_timeout',{}),('keyboard_input_failed',{'partial_input':True})]:
            self.runner.execute_action=Mock(side_effect=trial.WindowError('fake unknown dispatch',code,details))
            report=self.run_responses([response(action())]*20)
            self.assertEqual(report['status'],'input_outcome_unknown')
            self.assertEqual(len(self.runner.client.requests),1)
            self.assertIsNone(report['last_error']['executed'])
            self.assertTrue(report['last_error']['submission_unknown'])

    def test_non_input_loop_is_bounded(self):
        report=self.run_responses([response(action('switch',surface='game'))]*20)
        self.assertEqual(report['status'],'no_progress')
        self.assertLessEqual(len(self.runner.client.requests),6)

    def test_started_execute_waits_continue_past_six_without_claiming_completion(self):
        self.runner.started=True
        self.runner.cancel.wait=Mock(return_value=False)
        report=self.run_responses([response(action('wait',seconds=1))]*8+
                                  [response(action('ask'))],mode='execute')
        self.assertEqual(report['status'],'needs_input')
        self.assertEqual(report['steps'],9)
        self.assertEqual(report['inputs'],0)

    def test_started_execute_can_switch_between_bound_windows_past_six(self):
        self.runner.started=True
        self.runner.adapters={'maa':object(),'game':object()}
        switches=[response(action('switch',surface=surface)) for surface in ('game','maa')*4]
        report=self.run_responses(switches+[response(action('ask'))],mode='execute')
        self.assertEqual(report['status'],'needs_input')
        self.assertEqual(report['steps'],9)
        self.assertEqual(report['inputs'],0)

    def test_prepare_and_unstarted_execute_waits_still_stop_after_six(self):
        self.runner.cancel.wait=Mock(return_value=False)
        for mode,started in [('prepare',True),('execute',False)]:
            with self.subTest(mode=mode,started=started):
                self.runner.started=started
                report=self.run_responses([response(action('wait',seconds=1))]*20,mode=mode)
                self.assertEqual(report['status'],'no_progress')
                self.assertEqual(report['steps'],6)

    def test_started_execute_unavailable_switches_still_stop_after_six(self):
        self.runner.started=True
        report=self.run_responses([response(action('switch',surface='game'))]*20,mode='execute')
        self.assertEqual(report['status'],'no_progress')
        self.assertEqual(report['steps'],6)

    def test_started_execute_repeated_input_failure_still_stops_after_three(self):
        self.runner.started=True
        self.runner.execute_action=Mock(side_effect=trial.WindowError('fake foreground failure','focus_denied'))
        report=self.run_responses([response(action())]*20,mode='execute')
        self.assertEqual(report['status'],'no_progress')
        self.assertEqual(report['steps'],3)
        self.assertEqual(report['inputs'],0)

    def test_monitor_waits_do_not_erase_no_change_input_stalls(self):
        self.runner.started=True
        self.runner.cancel.wait=Mock(return_value=False)
        def submit(*args):
            self.runner.input_count+=1
            return {'submitted':True,'observed_change':False}
        self.runner.execute_action=Mock(side_effect=submit)
        sequence=[response(action()),response(action('wait',seconds=1))]*10
        report=self.run_responses(sequence,mode='execute')
        self.assertEqual(report['status'],'no_progress')
        self.assertEqual(report['inputs'],6)
        self.assertEqual(report['steps'],11)

    def test_started_read_only_monitoring_respects_step_limit(self):
        self.runner.started=True
        self.runner.cancel.wait=Mock(return_value=False)
        report=self.run_responses([response(action('wait',seconds=1))]*20,
                                  max_steps=8,mode='execute')
        self.assertEqual(report['status'],'steps_exhausted')
        self.assertEqual(report['steps'],8)

    def test_started_read_only_monitoring_respects_twenty_minute_limit(self):
        self.runner.started=True
        self.runner.cancel.wait=Mock(return_value=False)
        with patch.object(trial.time,'monotonic',side_effect=[0,1,1201,1202]):
            report=self.run_responses([response(action('wait',seconds=1))]*20,mode='execute')
        self.assertEqual(report['status'],'time_budget_exhausted')
        self.assertEqual(report['inputs'],0)

    def test_research_error_returns_actual_legal_download_parameters(self):
        # Synthetic discovery exists only in this fake session; it is no task answer.
        discovered='https://prts.maa.plus/copilot/get/42'
        self.runner.research_tools=SimpleNamespace(
            discovered_copilot_urls={discovered},stats={'search':1,'fetch':0},
            download_copilot=Mock(side_effect=ValueError('fake invalid URL')))
        def research_response(kind,text):
            r=response({'action':kind,'text':text});r['tool_calls'][0]['function']['name']='research_step';return r
        self.runner.research_client=FakeClient([research_response('download_copilot','not a URL'),research_response('answer','unknown')])
        result=self.runner.research('discover independently')
        messages=self.runner.research_client.requests[1]
        content=json.loads(next(m['content'] for m in messages if m['role']=='tool'))
        self.assertEqual(content['tool_feedback']['download_copilot']['allowed_urls'],[discovered])
        self.assertIn('error',content)
        self.assertEqual(result['answer'],'unknown')

    def test_research_catalog_dispatches_model_chosen_query(self):
        catalog={'stage':'synthetic_stage_02','source':'https://prts.maa.plus/copilot/query?level_keyword=synthetic_stage_02',
                 'candidates':[{'title':'Synthetic candidate','source':'https://prts.maa.plus/copilot/get/42'}]}
        self.runner.research_tools=SimpleNamespace(stats={},discovered_copilot_urls=set(),
            catalog=Mock(return_value={'ok':True,'tool':'catalog','catalog':catalog}))
        def research_response(kind,text):
            r=response({'action':kind,'text':text});r['tool_calls'][0]['function']['name']='research_step';return r
        self.runner.research_client=FakeClient([
            research_response('catalog','synthetic_stage_02'),research_response('answer','actual candidate returned')])
        result=self.runner.research('discover independently')
        self.runner.research_tools.catalog.assert_called_once_with('synthetic_stage_02')
        self.assertEqual(result['catalog_results'],[catalog])

    def test_default_output_is_fresh_and_not_an_old_artifact(self):
        first,second=trial.default_output_directory(),trial.default_output_directory()
        self.assertNotEqual(first,second)
        self.assertEqual(first.parent,ROOT/'artifacts')
        self.assertNotEqual(first.name,'tl2-shu-autonomous-20261106-01')
        self.assertTrue(first.name.startswith('tl2-shu-gui-uia-'))

    def test_submitted_inputs_without_observed_change_do_not_reset_stall(self):
        def submit(*args):
            self.runner.input_count+=1
            return {'submitted':True,'observed_change':False}
        self.runner.execute_action=Mock(side_effect=submit)
        report=self.run_responses([response(action())]*20)
        self.assertEqual(report['status'],'no_progress')
        self.assertEqual(report['inputs'],6)
        self.assertEqual(len([r for r in self.runner.records if r['kind']=='action_no_progress']),6)

    def test_malformed_call_ids_stop_without_another_provider_request(self):
        malformed=response(action());malformed['tool_calls'][0].pop('id')
        report=self.run_responses([malformed]*20)
        self.assertEqual(report['status'],'model_protocol_invalid')
        self.assertEqual(len(self.runner.client.requests),1)

    def test_other_output_research_is_not_injected(self):
        old=self.runner.out/'tl2-shu-research-20261106-01';old.mkdir()
        copilot=old/'synthetic.json';copilot.write_text('{}')
        (old/'research-result.json').write_text(json.dumps({'origin':'shu_deepseek_research_tool_loop',
            'result':{'sources_and_tool_results':[{'result':{'path':str(copilot)}}]}}))
        self.runner.out=self.runner.out/'new-session';self.runner.out.mkdir()
        self.run_responses([response(action('ask'))])
        self.assertNotIn(str(copilot),self.runner.allowed_texts)
        self.assertNotIn('own_research',str(self.runner.client.requests))

    def setup_uia(self,name='安全页签'):
        element={'id':'uia:fake','name':name,'automation_id':'tab','control_type':'TabItem',
                 'enabled':True,'offscreen':False,'rect':{'left':1,'top':2,'right':3,'bottom':4},
                 'patterns':['invoke','select'],'value':None,'toggle':None,'selected':False}
        old={'id':'old-image','surface':'maa','window_id':7,'window_pid':8,'width':100,'height':100,
             'uia':{'snapshot_id':'old','elements':[element],'status':'available'}}
        fresh=copy.deepcopy(old);fresh['id']='fresh-image';fresh['uia']['snapshot_id']='fresh'
        after=copy.deepcopy(fresh);after['id']='after-image';after['uia']['snapshot_id']='after'
        after['uia']['elements'][0]['selected']=True
        self.runner.adapters={'maa':SimpleNamespace(set_input_enabled=Mock())}
        self.runner.uia=SimpleNamespace(act=Mock(return_value={'ok':True,'verification_required':True}))
        self.runner.approve=Mock(return_value=False)
        self.runner.cancel.wait=Mock(return_value=False)
        self.runner.observe=Mock(side_effect=[fresh,after])
        a=action('uia',text=json.dumps({'element_id':'uia:fake','action':'select','value':None}))
        return a,old,fresh,after

    def test_uia_uses_fresh_token_and_passes_cancel_without_foreground(self):
        a,old,_,_=self.setup_uia()
        result=self.runner.execute_action(a,old,'prepare','test')
        self.runner.observe.assert_any_call('maa',focus=False,restore=False)
        self.runner.adapters['maa'].set_input_enabled.assert_not_called()
        self.runner.uia.act.assert_called_once_with('fresh','uia:fake',action='select',value=None,cancel_event=self.runner.cancel)
        self.assertEqual(self.runner.input_count,1)
        self.assertTrue(result['observed_change'])

    def test_uia_start_name_is_blocked_in_prepare_even_with_navigate_purpose(self):
        for name in ('开始','Link Start','重新开始','Begin'):
            a,old,_,_=self.setup_uia(name)
            with self.assertRaises(trial.WindowError) as cm:self.runner.execute_action(a,old,'prepare','test')
            self.assertEqual(cm.exception.code,'start_not_allowed')
            self.runner.approve.assert_not_called();self.runner.uia.act.assert_not_called()

    def test_import_only_locally_blocks_game_input_and_start_before_review(self):
        a,old,_,_=self.setup_uia('开始')
        for forbidden in (a,action('click',surface='game'),action('key',purpose='start'),
                          action('click'),action('drag'),action('key',text='ENTER'),action('type',text='1')):
            with self.assertRaises(trial.WindowError):
                self.runner.execute_action(forbidden,old,'import_only','only import')
        self.runner.approve.assert_not_called();self.runner.uia.act.assert_not_called()

    def test_import_only_checker_does_not_require_game_roster_or_navigation(self):
        verdict={'allowed':True,'starts_battle':False,'reason':'only import'}
        r=response(verdict);r['tool_calls'][0]['function']['name']='check_gui_action'
        self.runner.checker=FakeClient([r])
        self.assertFalse(self.runner.approve(action(),{'id':'fake'},'import_only','only import'))
        messages=self.runner.checker.requests[0]
        self.assertIn('不要求阵容',messages[0]['content'])
        self.assertNotIn('只完成TL-2一次',str(messages))

    def test_import_only_cannot_set_global_whitelist_or_toggle_controls(self):
        a,old,_,_=self.setup_uia()
        for payload in ({'element_id':'uia:fake','action':'set_value','value':'1'},
                        {'element_id':'uia:fake','action':'toggle','value':None}):
            a['text']=json.dumps(payload)
            with self.assertRaises(trial.WindowError):
                self.runner.execute_action(a,old,'import_only','only import')
        self.runner.approve.assert_not_called();self.runner.uia.act.assert_not_called()

    def test_import_only_terminal_without_receipt_needs_review(self):
        self.runner.client=FakeClient([response(action('ready'))])
        report=self.runner.run({'mode':'import_only','goal':'only import','max_steps':1})
        self.assertEqual(report['status'],'needs_review')
        self.assertFalse(report['battle_start_submitted'])
        self.assertEqual(report['import_receipt']['status'],'needs_review')

    def test_import_only_probe_needs_only_maa_and_default_still_requires_game(self):
        window=SimpleNamespace(executable='MAA.exe',is_game_client=False,hwnd=7)
        finder=SimpleNamespace(list_windows=Mock(return_value=[window]))
        info=SimpleNamespace(to_dict=lambda:{'hwnd':7})
        controller=SimpleNamespace(bind=Mock(return_value=info))
        permission={'own_elevated':True,'requires_elevation':False}
        with patch.object(trial,'WindowController',side_effect=[finder,controller]), \
                patch.object(trial,'inspect_game_input_permission',return_value=permission):
            self.runner.probe(require_game=False)
        self.assertEqual(set(self.runner.adapters),{'maa'})
        self.runner.observe.assert_called_once_with('maa')
        with patch.object(trial,'WindowController',side_effect=[finder,controller]), \
                patch.object(trial,'inspect_game_input_permission',return_value=permission):
            with self.assertRaisesRegex(RuntimeError,'game'):
                self.runner.probe()

    def submitted_import_observation(self):
        folder=self.runner.out/'research'/'copilots';folder.mkdir(parents=True)
        path=folder/'synthetic.json'
        path.write_text(json.dumps({'stage_name':'synthetic_stage','doc':{'title':'Synthetic unique lesson'},
                                   'actions':[{'type':'Deploy'}]}),encoding='utf-8')
        result={'ok':True,'tool':'download_copilot','path':str(path),
                'source_url':'https://prts.maa.plus/copilot/get/42',
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        self.runner.research_tools=SimpleNamespace(discovered_copilot_urls={result['source_url']})
        self.runner.register_download(result)
        a,old,fresh,after=self.setup_uia()
        for frame in (old,fresh,after):frame['uia']['elements'][0].update(patterns=['set_value'],value='')
        after['uia']['elements'][0]['value']='42'
        a['text']=json.dumps({'element_id':'uia:fake','action':'set_value','value':'42'})
        self.runner.execute_action(a,old,'import_only','only import')
        return after

    def test_receipt_rejects_pending_download_even_with_matching_stage_title(self):
        after=self.submitted_import_observation()
        for text in ('正在下载作业 42，请稍候',
                     '正在加载 synthetic_stage Synthetic unique lesson，请稍候'):
            after['uia']['elements']=after['uia']['elements'][:1]+[{'name':text,'patterns':[],'offscreen':False}]
            self.assertEqual(self.runner.import_receipt(after)['status'],'needs_review')

    def test_receipt_rejects_failed_load_even_with_matching_stage_title(self):
        after=self.submitted_import_observation()
        for text in ('作业42加载失败','Synthetic unique lesson synthetic_stage 加载失败'):
            after['uia']['elements']=after['uia']['elements'][:1]+[{'name':text,'patterns':[],'offscreen':False}]
            self.assertEqual(self.runner.import_receipt(after)['status'],'needs_review')

    def test_receipt_accepts_completed_load_with_stage_and_title_evidence(self):
        after=self.submitted_import_observation()
        after['uia']['elements'].append({'name':'已加载作业：synthetic_stage / Synthetic unique lesson',
                                        'patterns':[],'offscreen':False})
        receipt=self.runner.import_receipt(after)
        self.assertEqual(receipt['status'],'import_verified')
        self.assertEqual({e['field'] for e in receipt['loaded_evidence']},{'stage_name','title'})

    def test_receipt_needs_real_download_set_value_and_new_loaded_static_text(self):
        folder=self.runner.out/'research'/'copilots';folder.mkdir(parents=True)
        path=folder/'synthetic.json'
        body={'stage_name':'synthetic_stage','doc':{'title':'Synthetic unique lesson'},'actions':[{'type':'Deploy'}]}
        path.write_text(json.dumps(body),encoding='utf-8')
        result={'ok':True,'tool':'download_copilot','path':str(path),
                'source_url':'https://prts.maa.plus/copilot/get/42',
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        self.runner.research_tools=SimpleNamespace(discovered_copilot_urls={result['source_url']})
        registered=self.runner.register_download(result)
        self.assertEqual(registered['title'],'Synthetic unique lesson')
        self.assertIn('42',self.runner.allowed_texts)
        self.assertEqual(self.runner.import_receipt({'id':'after'})['status'],'needs_review')
        a,old,fresh,after=self.setup_uia()
        for frame in (old,fresh,after):
            frame['uia']['elements'][0].update(patterns=['set_value'],value='')
        after['uia']['elements'][0]['value']='42'
        a['text']=json.dumps({'element_id':'uia:fake','action':'set_value','value':'42'})
        self.runner.execute_action(a,old,'import_only','only import')
        # Echoing the code in the editable input cannot prove a loaded document.
        self.assertEqual(self.runner.import_receipt(after)['status'],'needs_review')
        after['uia']['elements'].append({'id':'loaded-title','name':'Synthetic unique lesson',
            'patterns':[],'offscreen':False,'read_only':True})
        receipt=self.runner.import_receipt(after)
        self.assertEqual(receipt['status'],'import_verified')
        self.assertEqual(receipt['copilot_id'],'42')
        self.assertEqual(receipt['sha256'],result['sha256'])
        self.runner.research_facts=[{'result':{'catalog':{'stage':'synthetic_stage','candidates':[{'title':'Synthetic unique lesson'}]}}}]
        summary=self.runner.research_finished({'answer':'done'})
        self.assertEqual(summary['catalog_results'][0]['candidates'][0]['title'],'Synthetic unique lesson')
        self.assertEqual(summary['downloaded_copilots'][0]['copilot_id'],'42')
        path.write_text('{}',encoding='utf-8')
        self.assertEqual(self.runner.import_receipt(after)['status'],'needs_review')

    def test_uia_start_reserves_single_ledger_even_when_checker_misses_start(self):
        a,old,_,_=self.setup_uia('开始')
        self.runner.execute_action(a,old,'execute','test')
        self.assertTrue(self.runner.started)
        self.assertTrue((self.runner.out/'start-ledger.json').is_file())
        with self.assertRaises(trial.WindowError):self.runner.execute_action(a,old,'execute','test')
        self.assertEqual(self.runner.uia.act.call_count,1)

    def test_uia_change_during_review_rejects_before_act(self):
        a,old,fresh,after=self.setup_uia()
        fresh['uia']['elements'][0]['selected']=True
        self.runner.observe.side_effect=[fresh,after]
        with self.assertRaises(trial.WindowError) as cm:self.runner.execute_action(a,old,'prepare','test')
        self.assertEqual(cm.exception.code,'uia_target_changed')
        self.runner.uia.act.assert_not_called()

    def test_local_stop_prevents_uia_dispatch(self):
        a,old,_,_=self.setup_uia();self.runner.cancel.set()
        self.runner.adapters['maa'].set_stop=Mock()
        with self.assertRaises(RuntimeError):self.runner.execute_action(a,old,'prepare','test')
        self.runner.uia.act.assert_not_called()

    def test_zero_controls_and_no_patterns_are_explicitly_unsupported(self):
        for elements in ([],[{'id':'root','patterns':[]} ]):
            self.runner.uia_key=(7,8)
            self.runner.uia=SimpleNamespace(snapshot=Mock(return_value={'snapshot_id':'fake','elements':elements}))
            snapshot=self.runner.uia_observe({'window_id':7,'window_pid':8})
            self.assertEqual(snapshot['status'],'unsupported')
            self.assertEqual(snapshot['observed_element_count'],len(elements))

    def test_observe_does_not_focus_a_non_minimized_window(self):
        a,old,_,_=self.setup_uia()
        self.runner.uia_key=(7,8)
        self.runner.uia.snapshot=Mock(return_value=old['uia'])
        controller=self.runner.adapters['maa']
        controller.bound_window=Mock(return_value=SimpleNamespace(minimized=False))
        controller.focus_for_capture=Mock(side_effect=AssertionError('unexpected foreground request'))
        controller.capture=Mock(return_value=copy.deepcopy(old))
        result=trial.Trial.observe(self.runner,'maa')
        self.assertEqual(result['uia']['status'],'available')
        controller.focus_for_capture.assert_not_called()

    def test_review_rejects_all_multi_calls_then_recovers_with_feedback(self):
        def review_response(*values):
            r=response(*values)
            for call in r['tool_calls']:call['function']['name']='check_gui_action'
            return r
        verdict={'allowed':True,'starts_battle':False,'reason':'fake navigation'}
        self.runner.checker=FakeClient([review_response(verdict,verdict),review_response(verdict)])
        self.assertFalse(self.runner.approve(action(),{'id':'fake'},'prepare','test'))
        results=[m for m in self.runner.checker.requests[1] if m['role']=='tool']
        self.assertEqual([m['tool_call_id'] for m in results],['call-0','call-1'])
        self.assertTrue(all(json.loads(m['content'])['executed'] is False for m in results))

class UiaBoundaries(unittest.TestCase):
    def test_model_observation_preserves_controls_and_bounded_static_logs(self):
        controls=[{'id':'edit','name':'input','patterns':['set_value'],'enabled':True,'offscreen':False,'value':'42'}]
        texts=[{'id':'title','name':'Loaded unique title','patterns':[],'offscreen':False}]
        texts.extend({'id':str(i),'name':'x'*1000,'patterns':[],'offscreen':False} for i in range(20))
        observed=trial.model_observation({'uia':{'elements':controls+texts,'status':'available'}})
        self.assertEqual(observed['uia']['elements'],controls)
        self.assertIn('Loaded unique title',observed['uia']['static_text'])
        self.assertLessEqual(len(observed['uia']['static_text']),6000)

    def test_uia_value_uses_exact_whitelist(self):
        a=action('uia',text=json.dumps({'element_id':'runtime:fake','action':'set_value','value':'TL-2'}))
        self.assertEqual(trial.validate_action(a,{'TL-2'}),a)
        with self.assertRaises(ValueError):trial.validate_action(a,set())
        a['surface']='game'
        with self.assertRaises(ValueError):trial.validate_action(a,{'TL-2'})

    def test_uia_target_changes_are_rejected(self):
        element={'id':'fake','name':'运行次数','automation_id':'count','control_type':'Edit',
                 'enabled':True,'offscreen':False,'rect':[1,2,3,4],
                 'patterns':['Value'],'value':'2','toggle':None,'selected':None}
        snapshot={'snapshot_id':'old','elements':[element]}
        fresh=copy.deepcopy(snapshot);fresh['snapshot_id']='fresh'
        self.assertEqual(trial.stable_uia_target(snapshot,fresh,'fake'),element)
        for key,value in [('name','开始'),('value','1'),('rect',[2,2,3,4]),('toggle',True)]:
            changed=copy.deepcopy(fresh);changed['elements'][0][key]=value
            with self.assertRaises(trial.WindowError):trial.stable_uia_target(snapshot,changed,'fake')

class TrialBoundaries(unittest.TestCase):
    def test_result_files_never_become_commands(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for name in ('command-01.json','command-01-result.json','command-02.json'):(p/name).write_text('{}')
            self.assertEqual([x.name for x in trial.pending_commands(p,{'command-01.json'})],['command-02.json'])

    def test_accepted_command_never_replays_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'command-01.json').write_text('{}');(p/'command-01.accepted').write_text('once')
            self.assertEqual(trial.pending_commands(p,set()),[])

    def test_text_requires_locally_registered_value(self):
        a=dict(kind='type',surface='maa',x=0,y=0,to_x=0,to_y=0,text='123456',seconds=0,purpose='configure',summary='input')
        with self.assertRaises(ValueError):trial.validate_action(a,{'TL-2','1'})
        self.assertEqual(trial.validate_action(a,{'123456'}),a)

    def test_game_keyboard_and_nonfinite_coordinates_are_rejected(self):
        a=dict(kind='key',surface='game',x=0,y=0,to_x=0,to_y=0,text='ENTER',seconds=0,purpose='navigate',summary='key')
        with self.assertRaises(ValueError):trial.validate_action(a,set())
        a.update(kind='click',x=float('nan'))
        with self.assertRaises(ValueError):trial.validate_action(a,set())

if __name__=='__main__':unittest.main()

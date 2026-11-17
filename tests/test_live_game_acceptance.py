"""Acceptance helper tests: fake metadata/input callbacks, no desktop or API."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('live_game_acceptance',ROOT/'tools/live_game_acceptance.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class AcceptanceTests(unittest.TestCase):
    def args(self,*extra):
        return module.build_parser().parse_args(['--phase','stage','--goal','完成已解锁关卡一次',
            '--output','isolated',*extra])
    def test_default_is_metadata_prepare_and_explicit_prepare_wins_over_input_opt_in(self):
        args=self.args();self.assertFalse(args.allow_game_input)
        self.assertFalse(args.show_pet)
        self.assertTrue(self.args('--show-pet').show_pet)
        self.assertEqual(args.input_attempt_cap,32);self.assertIsNone(args.key_file)
        args=self.args('--allow-game-input','--prepare-only')
        self.assertFalse(args.allow_game_input and not args.prepare_only)
        with self.assertRaises(SystemExit):self.args('--input-attempt-cap','33')
    def test_phase_scope_keeps_goal_and_forbids_extra_resources_without_fake_battle_claim(self):
        stage=module.phase_goal('stage','进入1-7，只打一局')
        self.assertTrue(stage.startswith('进入1-7，只打一局'))
        for text in ['现有普通理智一次','不使用理智药','不购买','代理复现','不能声称自主部署']:
            self.assertIn(text,stage)
        base=module.phase_goal('base','收基建产物')
        self.assertIn('不换班',base);self.assertIn('不使用无人机',base);self.assertIn('不进入关卡',base)
        with self.assertRaises(ValueError):module.phase_goal('stage','')
    def test_prepare_reads_only_metadata_and_blocks_elevation_or_wrong_pid(self):
        window=SimpleNamespace(is_game_client=True,pid=42,minimized=False,
            to_dict=lambda:{'hwnd':11,'pid':42,'title':'明日方舟'})
        calls=[]
        adapter=SimpleNamespace(list_windows=lambda **kwargs:calls.append(kwargs) or [window])
        metadata=module.prepare_metadata(adapter,lambda _: {'requires_elevation':True,'own_elevated':False})
        self.assertEqual(metadata['status'],'blocked');self.assertEqual(metadata['error_code'],'elevation_required')
        self.assertEqual(metadata['game_inputs'],0);self.assertEqual(metadata['model_requests'],0)
        metadata=module.prepare_metadata(adapter,lambda _: {'requires_elevation':False,'own_elevated':True},expected_pid=99)
        self.assertEqual(metadata['error_code'],'unexpected_game_pid')
        self.assertTrue(all(call=={'game_only':True} for call in calls))
    def test_prepare_requires_one_real_client_and_known_own_permission(self):
        adapter=SimpleNamespace(list_windows=lambda **kwargs:[])
        self.assertEqual(module.prepare_metadata(adapter,lambda _: {})['error_code'],'game_not_unique')
        window=SimpleNamespace(is_game_client=True,pid=42,minimized=False,to_dict=lambda:{'pid':42})
        adapter.list_windows=lambda **kwargs:[window]
        self.assertEqual(module.prepare_metadata(adapter,lambda _: {})['error_code'],'permission_unknown')
        self.assertEqual(module.prepare_metadata(adapter,lambda _: {'own_elevated':True,'requires_elevation':False})['status'],'prepared')
    def test_usage_aggregation_counts_persona_none_task_and_nested_cache_without_double_counting_total(self):
        calls=[{'role':'persona','task_id':None,'metrics':{'latency_seconds':1.2,'usage':{'prompt_tokens':100,
                'completion_tokens':10,'total_tokens':110,'prompt_tokens_details':{'cached_tokens':80}}}},
            {'role':'game','task_id':'task','metrics':{'latency_seconds':2.,'usage':{'prompt_tokens':200,
                'completion_tokens':20,'total_tokens':220,'prompt_tokens_details':{'cached_tokens':100}}}},
            {'role':'game','metrics':{'usage':{}}}]
        result=module.usage_summary(calls)
        self.assertEqual(result['recorded_calls'],3);self.assertEqual(result['calls_by_role'],{'persona':1,'game':2})
        self.assertEqual(result['reported_usage_totals']['total_tokens'],330)
        self.assertEqual(result['reported_usage_totals']['prompt_tokens_details.cached_tokens'],180)
        self.assertEqual(result['calls_without_reported_usage'],1);self.assertFalse(result['usage_complete'])
        self.assertFalse(result['price_or_cost_estimated']);self.assertEqual(result['summed_latency_seconds'],3.2)
    def test_input_attempts_and_submitted_results_are_separate_and_drag_shares_cap(self):
        from shu.windows import WindowError
        budget=module.InputBudget(2)
        def rejected(*args,**kwargs):raise WindowError('fixture rejection','pointer_displaced')
        with self.assertRaises(WindowError):budget.wrap('click',rejected)(1,2,'frame-1')
        result=budget.wrap('drag',lambda *a,**k:{'ok':True,'native_evidence':{'button_released':True}})(1,2,3,4,.4,'frame-2')
        self.assertTrue(result['ok'])
        reached=[]
        with self.assertRaises(WindowError):budget.wrap('click',lambda *a,**k:reached.append(1))(1,2,'frame-3')
        records=budget.snapshot()
        self.assertEqual(len(records['attempts']),2);self.assertEqual(len(records['submitted']),1)
        self.assertEqual(records['attempts'][0]['error_code'],'pointer_displaced')
        self.assertEqual(records['attempts'][1]['kind'],'drag');self.assertEqual(reached,[])
    def test_report_redaction_discards_hidden_reasoning_credentials_and_binary_samples(self):
        clean=module.redact({'api_key':'fixture','reasoning_content':'private','usage':{'total_tokens':12},
            'nested':{'authorization':'Bearer sk-fixtureabcdef','sample_rgb':b'pixels'},'error':'Key sk-fixtureabcdef failed'})
        self.assertNotIn('api_key',clean);self.assertNotIn('reasoning_content',clean)
        self.assertEqual(clean['nested'],{});self.assertNotIn('sk-fixtureabcdef',clean['error'])
        self.assertEqual(clean['usage'],{'total_tokens':12})
    def test_stop_file_requests_production_stop_once_and_keeps_flag_for_review(self):
        calls=[]
        controller=SimpleNamespace(stop=lambda **kwargs:calls.append(kwargs))
        with tempfile.TemporaryDirectory() as folder:
            flag=Path(folder)/'STOP.flag'
            gate=module.StopFileGate(flag,lambda:controller.stop(silent=True))
            self.assertFalse(gate.poll());self.assertEqual(calls,[])
            flag.write_text('stop',encoding='utf-8')
            self.assertTrue(gate.poll());self.assertEqual(calls,[{'silent':True}])
            self.assertFalse(gate.poll());self.assertTrue(flag.is_file())
            self.assertEqual(calls,[{'silent':True}])
    def test_late_before_capture_cannot_submit_after_stop_or_controller_close(self):
        calls=[]
        controller=SimpleNamespace(_closed=False,submit=lambda goal:calls.append(goal),
                                   stop=lambda **kwargs:calls.append(('stop',kwargs)))
        state={'stopping':False,'stop_requested':False}
        def stop():
            state.update(stopping=True,stop_requested=True);controller.stop(silent=True)
        with tempfile.TemporaryDirectory() as folder:
            flag=Path(folder)/'STOP.flag';gate=module.StopFileGate(flag,stop)
            flag.write_text('stop',encoding='utf-8');gate.poll();gate.poll()
            self.assertFalse(module.submit_initial_goal(controller,'must not start',**state))
        self.assertEqual(calls,[('stop',{'silent':True})])
        for blocked in [{'stop_requested':True},{'stopping':True},{'finishing':True}]:
            self.assertFalse(module.submit_initial_goal(controller,'late frame',**blocked))
        controller._closed=True
        self.assertFalse(module.submit_initial_goal(controller,'closed session'))
        self.assertEqual(calls,[('stop',{'silent':True})])
        controller._closed=False
        self.assertTrue(module.submit_initial_goal(controller,'normal initial capture'))
        self.assertEqual(calls[-1],'normal initial capture')
    def test_headless_finish_closes_production_controller_and_quits_without_a_window(self):
        calls=[]
        controller=SimpleNamespace(close=lambda:calls.append('controller.close'))
        app=SimpleNamespace(quit=lambda:calls.append('app.quit'))
        module.close_acceptance(controller,app)
        self.assertEqual(calls,['controller.close','app.quit'])
        calls.clear()
        window=SimpleNamespace(quit_application=lambda:calls.append('window.quit_application'))
        module.close_acceptance(controller,app,window)
        self.assertEqual(calls,['window.quit_application'])


if __name__=='__main__':unittest.main()

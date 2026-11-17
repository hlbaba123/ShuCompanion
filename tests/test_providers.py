"""Provider contracts: no live API, local credentials, or installed SDK needed."""
from __future__ import annotations

import copy
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from shu.providers import (
    DeepSeekClient, GameSession, HarnessBackend, PersonaSession,
    ProviderCancelled, ProviderConfig, ProviderError, load_api_key,
)


TEST_KEY = "sk-unit-test-credential-only"
TOOLS = [{"type": "function", "function": {"name": "observe_window", "description": "Observe",
          "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]


def response_bytes(message: dict | None = None, **extra) -> bytes:
    payload = {"choices": [{"message": message or {"role": "assistant", "content": "已收到。"},
                             "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools=None, cancel_event=None, tool_choice=None):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools),
                           "tool_choice": copy.deepcopy(tool_choice)})
        if not self.responses:
            raise AssertionError("Unexpected additional model request")
        return copy.deepcopy(self.responses.pop(0))


def png_fixture(width=32, height=16):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImage
    image=QImage(width,height,QImage.Format.Format_RGB32);image.fill(0xffbc9876)
    data=QByteArray();buffer=QBuffer(data);buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer,'PNG')
    return bytes(data)


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test-key.txt"

    def test_missing_empty_and_comment_files_are_empty(self):
        self.assertEqual(load_api_key(self.path), "")
        for content in ("", "\n# 说明\n; comment\n// comment\n"):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual(load_api_key(self.path), "")

    def test_bom_plain_key(self):
        self.path.write_text("# 本地测试\n" + TEST_KEY + "\n", encoding="utf-8-sig")
        self.assertEqual(load_api_key(self.path), TEST_KEY)

    def test_utf16_variable_and_quoted_values(self):
        for quote in ("'", '"'):
            self.path.write_text(f"# 测试\nDEEPSEEK_API_KEY = {quote}{TEST_KEY}{quote}\n", encoding="utf-16")
            self.assertEqual(load_api_key(self.path), TEST_KEY)

    def test_export_form_and_placeholders(self):
        self.path.write_text("export DEEPSEEK_API_KEY=" + TEST_KEY, encoding="utf-8")
        self.assertEqual(load_api_key(self.path), TEST_KEY)
        for content in ("DEEPSEEK_API_KEY=", "your_api_key_here", "请在这里填写 API Key", "sk-xxxxxxxx"):
            self.path.write_text(content, encoding="utf-8")
            self.assertEqual(load_api_key(self.path), "")

    def test_invalid_value_error_does_not_echo_file(self):
        secret = TEST_KEY + " extra-secret"
        self.path.write_text(secret, encoding="utf-8")
        with self.assertRaises(ProviderError) as caught:
            load_api_key(self.path)
        self.assertNotIn(TEST_KEY, str(caught.exception))
        self.assertNotIn("extra-secret", str(caught.exception))

    def test_config_key_priority_and_secret_repr(self):
        self.path.write_text(TEST_KEY, encoding="utf-8")
        config = ProviderConfig(api_key="explicit-test-key", api_key_file=self.path)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "environment-test-key"}):
            self.assertEqual(config.resolve_key(), "explicit-test-key")
            config.api_key = None
            self.assertEqual(config.resolve_key(), TEST_KEY)
            config.api_key_file = None
            self.assertEqual(config.resolve_key(), "environment-test-key")
        self.assertNotIn("explicit-test-key", repr(ProviderConfig(api_key="explicit-test-key")))


class DirectClientTests(unittest.TestCase):
    def setUp(self):
        self.client = DeepSeekClient(ProviderConfig(api_key=TEST_KEY))
        self.client._opener = Mock()

    def test_http_contract_multimodal_tools_and_reasoning_preserved(self):
        assistant = {"role": "assistant", "content": None, "reasoning_content": "模型推理字段",
                     "tool_calls": [{"id": "call-1", "type": "function", "function":
                                     {"name": "observe_window", "arguments": "{}"}}]}
        self.client._opener.open.return_value = io.BytesIO(response_bytes(assistant))
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "查看游戏"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8=", "detail": "original"}},
        ]}]
        actual = self.client.complete(messages, TOOLS)
        req = self.client._opener.open.call_args.args[0]
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(req.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + TEST_KEY)
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"], messages)
        self.assertEqual(body["tools"], TOOLS)
        self.assertEqual(actual, assistant)
        self.assertEqual(self.client.last_usage["completion_tokens"], 3)

    def test_v1_endpoint_is_retained(self):
        self.client.config.base_url = "http://localhost:9000/v1/"
        self.client._opener.open.return_value = io.BytesIO(response_bytes())
        self.client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(self.client._opener.open.call_args.args[0].full_url,
                         "http://localhost:9000/v1/chat/completions")

    def test_http_compaction_preserves_annotated_system_tool_reasoning_and_input(self):
        self.client._opener.open.return_value=io.BytesIO(response_bytes())
        messages=[{'role':'system','content':'角色'}, {'role':'system','content':'事实'},
            {'role':'system','content':'注释','name':'retained'}, {'role':'system','content':'独立'},
            {'role':'user','content':'原话'}, {'role':'assistant','content':None,'reasoning_content':'完整推理',
                'tool_calls':[{'id':'x','type':'function','function':{'name':'observe_window','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'x','content':'真实结果'},
            {'role':'system','content':'后续事实'}, {'role':'system','content':'后续约束'}]
        original=copy.deepcopy(messages)
        self.client.complete(messages,TOOLS)
        actual=json.loads(self.client._opener.open.call_args.args[0].data)['messages']
        self.assertEqual(messages,original)
        self.assertEqual(actual,[{'role':'system','content':'角色\n\n事实'},*original[2:7],
                                 {'role':'system','content':'后续事实\n\n后续约束'}])

    def test_forced_tool_routes_in_one_request_and_thinking_rejects_invalid_choice(self):
        self.client._opener.open.return_value=io.BytesIO(response_bytes())
        choice={'type':'function','function':{'name':'observe_window'}}
        self.client.complete([{'role':'user','content':'查看'}],TOOLS,tool_choice=choice)
        self.assertEqual(json.loads(self.client._opener.open.call_args.args[0].data)['tool_choice'],choice)
        self.client._opener.open.reset_mock();self.client.config.thinking=True
        with self.assertRaises(ProviderError):
            self.client.complete([{'role':'user','content':'查看'}],TOOLS,tool_choice=choice)
        self.client._opener.open.assert_not_called()

    def test_metrics_record_actual_cache_usage_size_and_latency_without_prices(self):
        usage={'prompt_tokens':100,'completion_tokens':20,'prompt_cache_hit_tokens':80,
               'prompt_cache_miss_tokens':20,'completion_tokens_details':{'reasoning_tokens':12},
               'arbitrary_text':TEST_KEY}
        self.client._opener.open.return_value=io.BytesIO(response_bytes(usage=usage))
        self.client.complete([{'role':'user','content':'hello'}])
        metrics=self.client.last_metrics
        self.assertEqual(metrics['usage']['prompt_cache_hit_tokens'],80)
        self.assertEqual(metrics['usage']['completion_tokens_details']['reasoning_tokens'],12)
        self.assertGreaterEqual(metrics['latency_seconds'],0)
        self.assertGreater(metrics['request_bytes'],0);self.assertGreater(metrics['response_bytes'],0)
        self.assertNotIn(TEST_KEY,json.dumps(metrics));self.assertNotIn('price',metrics)
        self.assertEqual(len(self.client.metrics_history),1)

    def test_remote_http_or_credential_urls_rejected_before_request(self):
        for url in ("http://example.com", "https://user:password@example.com", "https://example.com?key=secret",
                    "https://[invalid", "https://example.com:badport", "https://example.com/ has-space"):
            self.client.config.base_url = url
            with self.subTest(url=url), self.assertRaises(ProviderError):
                self.client.complete([{"role": "user", "content": "hi"}])
        self.client._opener.open.assert_not_called()

    def test_bad_tool_schema_does_not_escape_as_attribute_error(self):
        for tools in ([None], [{"type": "function", "function": None}],
                      [{"type": "function", "function": {"name": 123}}],
                      [{"type": "function", "function": {"name": "invalid name"}}]):
            with self.subTest(tools=tools), self.assertRaises(ProviderError):
                self.client.complete([{"role": "user", "content": "hi"}], tools)
        self.client._opener.open.assert_not_called()

    def test_http_error_never_exposes_key(self):
        for status in (400, 401, 403, 429, 500):
            self.client._opener.open.side_effect = HTTPError(
                "https://api.deepseek.com/chat/completions", status, "error", {},
                io.BytesIO(json.dumps({"error": {"message": "bad value " + TEST_KEY}}).encode()))
            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                self.client.complete([{"role": "user", "content": "hi"}])
            self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_transient_http_failure_retries_same_model_request_without_replaying_tools(self):
        failure=lambda code:HTTPError('https://api.deepseek.com/chat/completions',code,'upstream',{},io.BytesIO(b'{}'))
        self.client._opener.open.side_effect=[failure(500),failure(502),io.BytesIO(response_bytes())]
        answer=self.client.complete([{'role':'user','content':'只描述任务'}],TOOLS)
        self.assertEqual(answer['content'],'已收到。')
        calls=self.client._opener.open.call_args_list
        self.assertEqual(len(calls),3)
        self.assertTrue(all(item.args[0].data==calls[0].args[0].data for item in calls))
        self.assertEqual(self.client.last_metrics['retry_count'],2)
        self.assertEqual([x.get('http_status') for x in self.client.last_metrics['request_attempts']],[500,502,200])

    def test_retry_budget_does_not_retry_client_errors_or_unbounded_upstream_failure(self):
        for code,expected in ((400,1),(401,1),(403,1),(429,3),(500,3),(503,3),(504,3)):
            self.client._opener.open.reset_mock()
            self.client._opener.open.side_effect=lambda *_a,**_kw:(_ for _ in ()).throw(
                HTTPError('https://api.deepseek.com/chat/completions',code,'upstream',{},io.BytesIO(b'{}')))
            with self.subTest(code=code),self.assertRaises(ProviderError):
                self.client.complete([{'role':'user','content':'测试'}])
            self.assertEqual(self.client._opener.open.call_count,expected)

    def test_cancel_during_retry_backoff_does_not_issue_next_request(self):
        cancel=threading.Event()
        def fail(*_args,**_kwargs):
            cancel.set()
            raise HTTPError('https://api.deepseek.com/chat/completions',503,'upstream',{},io.BytesIO(b'{}'))
        self.client._opener.open.side_effect=fail
        with self.assertRaises(ProviderCancelled):
            self.client.complete([{'role':'user','content':'测试'}],cancel_event=cancel)
        self.assertEqual(self.client._opener.open.call_count,1)

    def test_protocol_probe_can_disable_http_retries_without_changing_model_payload(self):
        self.client.TRANSIENT_RETRIES=0
        self.client._opener.open.side_effect=HTTPError('https://api.deepseek.com/chat/completions',500,
            'upstream',{},io.BytesIO(b'{}'))
        with self.assertRaises(ProviderError):self.client.complete([{'role':'user','content':'测试'}])
        self.assertEqual(self.client._opener.open.call_count,1)
        self.assertEqual(self.client.last_metrics['retry_count'],0)
        self.assertNotIn('TRANSIENT_RETRIES',json.loads(self.client._opener.open.call_args.args[0].data))

    def test_api_error_body_and_network_exception_are_redacted(self):
        self.client._opener.open.return_value = io.BytesIO(
            response_bytes(error={"message": TEST_KEY + " Bearer other-sensitive-value"}))
        with self.assertRaises(ProviderError) as caught:
            self.client.complete([{"role": "user", "content": "hi"}])
        self.assertNotIn(TEST_KEY, str(caught.exception))
        self.assertNotIn("other-sensitive-value", str(caught.exception))
        self.client._opener.open.side_effect = URLError("network error " + TEST_KEY)
        with self.assertRaises(ProviderError) as caught:
            self.client.complete([{"role": "user", "content": "hi"}])
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_malformed_response_is_safe_error(self):
        for raw in (b"not json", b"[]", b'{"choices":[]}',
                    response_bytes({"role": "assistant", "content": 17}),
                    response_bytes({"role": "assistant", "content": None})):
            self.client._opener.open.return_value = io.BytesIO(raw)
            with self.subTest(raw=raw), self.assertRaises(ProviderError):
                self.client.complete([{"role": "user", "content": "hi"}])

    def test_pre_cancelled_request_never_opens_network(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            self.client.complete([{"role": "user", "content": "hi"}], cancel_event=cancel)
        self.client._opener.open.assert_not_called()

    def test_cancel_discards_late_response_and_usage(self):
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()

        def slow_open(*args, **kwargs):
            entered.set()
            release.wait(5)
            return io.BytesIO(response_bytes())

        def request_cancel():
            if entered.wait(2):
                cancel.set()

        self.client._opener.open.side_effect = slow_open
        thread = threading.Thread(target=request_cancel, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises(ProviderCancelled):
                self.client.complete([{"role": "user", "content": "hi"}], cancel_event=cancel)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(self.client.last_usage, {})
        finally:
            release.set()
            thread.join(2)


class SessionTests(unittest.TestCase):
    def test_persona_keeps_long_current_contract_tail_and_latest_user_request_complete(self):
        current = {'task': {'goal': '已登记的完整目标和上下文' * 1600},
            'task_contract': {'constraints': ['禁止开战', '禁止花费源石'],
                'task_spec': {'medicine_limit': 0, 'premium_limit': 0},
                'unknowns': ['活动开放时间尚未核实'],
                'source_url': 'https://prts.wiki/w/Fixture#exact-evidence'},
            'battle_start_allowed': False}
        latest = '本条完整用户要求' * 2000 + '；最后限制：只查询，不导入，不开战。'
        encoded = json.dumps(current, ensure_ascii=False)
        self.assertGreater(len(encoded), 16000)
        self.assertGreater(len(latest), 16000)
        for summary in (current, encoded):
            with self.subTest(summary_type=type(summary).__name__):
                client = ScriptedClient({'role': 'assistant', 'content': '只查询。'})
                session = PersonaSession(client)
                before = copy.deepcopy(current)
                session.respond(latest, task_summary=summary)
                sent = client.calls[0]['messages']
                contexts = [message['content'] for message in sent[1:] if message['role'] == 'system']
                self.assertEqual(len(contexts), 1)
                self.assertTrue(contexts[0].endswith(encoded), 'current contract was silently prefix-clipped')
                self.assertEqual(json.loads(contexts[0].split('\n', 1)[1]), before)
                self.assertEqual(sent[-1], {'role': 'user', 'content': latest})
                self.assertEqual(current, before)
                self.assertEqual(session.messages[1]['content'], latest)

    def test_role_and_game_contexts_are_independent(self):
        client = ScriptedClient({"role": "assistant", "content": "今天也辛苦了。"},
                                {"role": "assistant", "content": "请先获取当前窗口观察。"})
        persona, game = PersonaSession(client), GameSession(client)
        self.assertEqual(persona.reply("只有闲聊的个人信息", task_summary="任务未开始"), "今天也辛苦了。")
        game.decide("只观察游戏", {"state": "unknown"}, TOOLS)
        role_payload, game_payload = [json.dumps(call["messages"], ensure_ascii=False) for call in client.calls]
        self.assertIn("只有闲聊的个人信息", role_payload)
        self.assertNotIn("只有闲聊的个人信息", game_payload)
        self.assertNotIn("今天也辛苦了", game_payload)
        self.assertNotIn("任务未开始", json.dumps(persona.messages, ensure_ascii=False))
        self.assertIsNot(persona.messages, game.messages)

    def test_persona_tool_loop_retains_reasoning_and_real_result(self):
        call = {"role": "assistant", "content": None, "reasoning_content": "需要提交目标",
                "tool_calls": [{"id": "submit-1", "type": "function", "function":
                                {"name": "submit_goal", "arguments": '{"goal":"观察"}'}}]}
        client = ScriptedClient(call, {"role": "assistant", "content": "目标已提交，等待观察。"})
        session = PersonaSession(client)
        self.assertEqual(session.respond("帮我看看游戏", tools=TOOLS), call)
        with self.assertRaises(ProviderError):
            session.respond("", tools=TOOLS)
        with self.assertRaises(ProviderError):
            session.record_tool_result("unknown", {"ok": True})
        session.record_tool_result("submit-1", {"accepted": True, "executed": False})
        answer = session.respond("", tools=TOOLS)
        self.assertIn("目标已提交", answer["content"])
        messages = client.calls[-1]["messages"]
        self.assertEqual(messages[-2]["reasoning_content"], "需要提交目标")
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(json.loads(messages[-1]["content"])["executed"], False)
        self.assertEqual(sum(m["role"] == "user" for m in session.messages), 1)

    def test_game_tool_result_required_and_reasoning_carried_forward(self):
        call = {"role": "assistant", "content": None, "reasoning_content": "先观察",
                "tool_calls": [{"id": "observe-1", "type": "function", "function":
                                {"name": "observe_window", "arguments": "{}"}}]}
        client = ScriptedClient(call, {"role": "assistant", "content": "当前状态仍未知。"})
        game = GameSession(client)
        game.decide("看看游戏", {"state": "unknown"}, TOOLS)
        with self.assertRaises(ProviderError):
            game.decide("看看游戏", {"state": "unknown"}, TOOLS)
        game.record_tool_result("observe-1", {"captured": True, "state": "unknown"})
        game.decide("看看游戏", {"state": "unknown"}, TOOLS)
        self.assertEqual(client.calls[-1]["messages"][2]["reasoning_content"], "先观察")
        self.assertEqual(client.calls[-1]["messages"][3]["role"], "tool")

    def test_game_explicit_history_starts_fresh_and_image_is_attached(self):
        client = ScriptedClient({"role": "assistant", "content": "先观察。"},
                                {"role": "assistant", "content": "看到了当前截图。"})
        game = GameSession(client)
        game.decide("旧目标", {"state": "unknown"}, TOOLS)
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "frame.png"
            image.write_bytes(png_fixture())
            game.decide("新目标", {"image_path": image, "state": "captured"}, TOOLS, history=[])
        latest = client.calls[-1]["messages"]
        self.assertNotIn("旧目标", json.dumps(latest, ensure_ascii=False))
        self.assertEqual(latest[-1]["content"][1]["type"], "image_url")
        self.assertEqual(latest[-1]["content"][1]["image_url"]["detail"], "original")

    def test_persona_forced_route_is_a_single_model_request(self):
        from shu.task_contract import ROUTE_TOOL_CHOICE, ROUTE_USER_TOOL
        client=ScriptedClient({'role':'assistant','content':None,'tool_calls':[{'id':'route',
            'type':'function','function':{'name':'route_user_request','arguments':'{}'}}]})
        session=PersonaSession(client)
        session.respond('继续',tools=[ROUTE_USER_TOOL],tool_choice=ROUTE_TOOL_CHOICE)
        self.assertEqual(len(client.calls),1)
        self.assertEqual(client.calls[0]['tool_choice'],ROUTE_TOOL_CHOICE)
        session.record_tool_result('route',{'accepted':True})

    def test_persona_continuation_is_explicit_and_cannot_bypass_pending_tools(self):
        client=ScriptedClient({'role':'assistant','content':'缺少结构化路由'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'route','type':'function',
                'function':{'name':'route_user_request','arguments':'{}'}}]})
        session=PersonaSession(client)
        with self.assertRaises(ProviderError):session.respond('',allow_continuation=True)
        session.respond('真实原话')
        with self.assertRaises(ProviderError):session.respond('')
        session.respond('',allow_continuation=True)
        self.assertEqual(sum(x['role']=='user' for x in session.messages),1)
        with self.assertRaises(ProviderError):session.respond('',allow_continuation=True)

    def test_persona_compacts_adjacent_system_context_without_mutating_history(self):
        client=DeepSeekClient(ProviderConfig(api_key=TEST_KEY))
        client._opener=Mock();client._opener.open.return_value=io.BytesIO(response_bytes())
        session=PersonaSession(client)
        initial=copy.deepcopy(session.messages)
        session.respond('真实要求',task_summary='已验证事实：只读')
        request=json.loads(client._opener.open.call_args.args[0].data)['messages']
        self.assertEqual([x['role'] for x in request],['system','user'])
        self.assertTrue(request[0]['content'].startswith(initial[0]['content']))
        self.assertIn('已验证事实：只读',request[0]['content'])
        self.assertEqual(session.messages[0],initial[0])
        self.assertEqual([x['role'] for x in session.messages],['system','user','assistant'])

    def test_vision_budget_keeps_full_aspect_and_original_capture_unchanged(self):
        from PySide6.QtGui import QImage
        client=ScriptedClient({'role':'assistant','content':'已观察'})
        game=GameSession(client);original=png_fixture(1920,1080)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'frame.png';path.write_bytes(original)
            game.decide('只观察',{'path':str(path),'width':1920,'height':1080},TOOLS)
            self.assertEqual(path.read_bytes(),original)
        block=client.calls[-1]['messages'][-1]['content'][1]['image_url']
        self.assertTrue(block['url'].startswith('data:image/jpeg;base64,'))
        sent=QImage.fromData(base64.b64decode(block['url'].partition(',')[2]))
        self.assertEqual((sent.width(),sent.height()),(1280,720))
        self.assertEqual(game.last_image_metrics['source_width'],1920)
        self.assertEqual(game.last_image_metrics['jpeg_quality'],80)

    def test_game_history_keeps_latest_pixels_and_complete_retained_reasoning(self):
        responses=[{'role':'assistant','content':'决策'+str(i),'reasoning_content':'完整推理'+str(i)} for i in range(4)]
        client=ScriptedClient(*responses);game=GameSession(client)
        image=base64.b64encode(png_fixture()).decode('ascii')
        for number in range(4):
            game.decide('目标'+str(number),{'image_base64':image,'execution_context':{
                'recent_steps':[{'step':i,'reasoning_content':'不应回传','image_base64':image} for i in range(20)]}},TOOLS)
        messages=client.calls[-1]['messages']
        self.assertEqual(sum(m['role']=='user' for m in messages),2)
        self.assertEqual(sum(b.get('type')=='image_url' for m in messages if isinstance(m.get('content'),list)
                             for b in m['content']),1)
        self.assertEqual(next(m['reasoning_content'] for m in messages if m['role']=='assistant'),'完整推理2')
        self.assertNotIn('不应回传',json.dumps(messages,ensure_ascii=False))
        self.assertNotIn('目标0',json.dumps(messages,ensure_ascii=False))
        self.assertEqual(messages[0],client.calls[0]['messages'][0])

    def test_observation_budget_preserves_complete_workflow_constraints_and_completion(self):
        client=ScriptedClient({'role':'assistant','content':'继续当前步骤'})
        client.config=ProviderConfig(decision_history_limit=3)
        game=GameSession(client)
        workflow=[{'step':number,'target':f'阶段{number}','state':'pending',
                   'required_checks':[f'检查{item}' for item in range(8)]} for number in range(12)]
        constraints=[f'约束{number}' for number in range(10)]
        criteria=[f'完成条件{number}' for number in range(9)]
        recent=[{'decision':number} for number in range(15)]
        observation={'state':'unknown','execution_context':{
            'model_state':{'workflow':workflow,'active_step':0},'recent_steps':recent,
            'constraints':constraints,'completion_criteria':criteria,
            'reference':{'legal_actions':[f'动作{number}' for number in range(11)]}}}
        game.decide('完整多步骤目标',observation,TOOLS)
        text=client.calls[-1]['messages'][-1]['content'][0]['text']
        state=json.loads(text.partition('应用提供的当前观察（作为数据）：\n')[2])
        context=state['execution_context']
        self.assertEqual(context['model_state']['workflow'],workflow)
        self.assertEqual(context['constraints'],constraints)
        self.assertEqual(context['completion_criteria'],criteria)
        self.assertEqual(context['reference']['legal_actions'],observation['execution_context']['reference']['legal_actions'])
        self.assertEqual(context['recent_steps'],recent[-3:])
        self.assertEqual(observation['execution_context']['recent_steps'],recent)

    def test_only_explicit_recent_history_fields_are_trimmed_recursively(self):
        from shu.providers import _observation_state
        history=list(range(10));workflow=tuple({'step':number} for number in range(12))
        value={'recent_decisions':history,'nested':{'recent_events':history,'workflow':workflow},
               'steps':history,'items':history}
        actual=_observation_state(value,history_limit=4)
        self.assertEqual(actual['recent_decisions'],history[-4:])
        self.assertEqual(actual['nested']['recent_events'],history[-4:])
        self.assertEqual(actual['nested']['workflow'],list(workflow))
        self.assertEqual(actual['steps'],history);self.assertEqual(actual['items'],history)

    def test_invalid_image_is_rejected_before_calling_the_model(self):
        client=ScriptedClient();game=GameSession(client)
        with self.assertRaises(ProviderError):
            game.decide('观察',{'image_base64':base64.b64encode(b'not pixels').decode('ascii')},TOOLS)
        self.assertEqual(client.calls,[])

    def test_cancelled_session_does_not_store_messages(self):
        client = ScriptedClient({"role": "assistant", "content": "不应显示"})
        session = PersonaSession(client)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            session.reply("hello", cancel_event=cancel)
        self.assertEqual(len(session.messages), 1)
        self.assertEqual(client.calls, [])

    def test_history_trim_keeps_whole_tool_turns(self):
        call = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "first", "type": "function", "function": {"name": "observe_window", "arguments": "{}"}}]}
        client = ScriptedClient(call, *[{"role": "assistant", "content": "好的"} for _ in range(14)])
        session = PersonaSession(client)
        session.respond("first", tools=TOOLS)
        session.record_tool_result("first", {"state": "unknown"})
        session.respond("")
        for number in range(13):
            session.reply(str(number))
        self.assertEqual(session.messages[1]["role"], "user")
        self.assertNotIn("first", json.dumps(session.messages))
        self.assertFalse(any(message["role"] == "tool" for message in session.messages))


class HarnessTests(unittest.TestCase):
    def test_unavailable_sdk_is_explicit_and_not_a_direct_api_fallback(self):
        backend = HarnessBackend(ProviderConfig(api_key=TEST_KEY))
        with patch("shu.providers.importlib.util.find_spec", return_value=None):
            availability = backend.availability()
            self.assertFalse(availability["installed"])
            self.assertFalse(availability["game_tools_verified"])
            with self.assertRaises(ProviderError) as caught:
                backend.run("hello")
        self.assertIn("没有启动 Harness", str(caught.exception))

    def test_installed_sdk_runs_real_constructor_contract(self):
        module = ModuleType("deepseek_harness")
        runtime = Mock()
        runtime.run.return_value = SimpleNamespace(final_response="SDK 真正返回的文本", finish_reason="stop")
        module.DeepSeekHarness = Mock(return_value=runtime)
        with tempfile.TemporaryDirectory() as temp:
            config = ProviderConfig(api_key=TEST_KEY, dsh_home=Path(temp) / "runtime",
                                    harness_profile="sdk", harness_patches=("custom-tools",))
            backend = HarnessBackend(config, workspace=Path(temp))
            with patch.dict("sys.modules", {"deepseek_harness": module}), \
                    patch.object(backend, "availability", return_value={"installed": True}):
                self.assertEqual(backend.run("你好", session_id="persona-test"), "SDK 真正返回的文本")
                constructor = module.DeepSeekHarness.call_args.kwargs
                self.assertEqual(constructor["dsh_home"], str((Path(temp) / "runtime").resolve()))
                self.assertEqual(constructor["cwd"], str(Path(temp).resolve()))
                self.assertEqual(constructor["profile"], "sdk")
                self.assertEqual(constructor["provider"], "deepseek-official")
                self.assertEqual(constructor["api_key"], TEST_KEY)
                self.assertEqual(constructor["patches"], ("custom-tools",))
                runtime.run.assert_called_once_with("你好", session_id="persona-test")
                backend.close()
                runtime.close.assert_called_once()

    def test_sdk_needs_explicit_home_and_redacts_exceptions(self):
        backend = HarnessBackend(ProviderConfig(api_key=TEST_KEY))
        with patch.object(backend, "availability", return_value={"installed": True}):
            with self.assertRaises(ProviderError) as caught:
                backend.run("hello")
            self.assertIn("本地数据目录", str(caught.exception))
        module = ModuleType("deepseek_harness")
        module.DeepSeekHarness = Mock(side_effect=RuntimeError("failure " + TEST_KEY))
        with tempfile.TemporaryDirectory() as temp:
            backend = HarnessBackend(ProviderConfig(api_key=TEST_KEY, dsh_home=Path(temp)))
            with patch.dict("sys.modules", {"deepseek_harness": module}), \
                    patch.object(backend, "availability", return_value={"installed": True}):
                with self.assertRaises(ProviderError) as caught:
                    backend.run("hello")
                self.assertNotIn(TEST_KEY, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

"""Offline dialogue integration; real PersonaSession, no provider or GUI IO."""
from __future__ import annotations

import copy
import importlib
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from shu.providers import PersonaSession, ProviderCancelled, ProviderError
from shu.research_tools import ResearchToolError


PAGE = 'https://prts.wiki/w/Fixture'


def assistant(*calls, content=None, **extra):
    value = {'role': 'assistant', 'content': content, **extra}
    if calls:
        value['tool_calls'] = list(calls)
    return value


def tool(identifier, name='web_search', **arguments):
    return {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, **options):
        self.requests.append({'messages': copy.deepcopy(messages), **options})
        if not self.responses:
            raise AssertionError('Unexpected extra model request')
        response = self.responses.pop(0)
        if callable(response):
            response = response()
        return copy.deepcopy(response)


class Research:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def _next(self, name, value):
        self.calls.append((name, value))
        if not self.results:
            raise AssertionError('Unexpected network request')
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result()
        return copy.deepcopy(result)

    def search(self, query):
        return self._next('search', query)

    def fetch(self, url):
        return self._next('fetch', url)


class NetworkDialogueTests(unittest.TestCase):
    def test_context_tail_reaches_model_without_character_prefix_clipping(self):
        module = importlib.import_module('shu.network_dialogue')
        client = ScriptedClient(assistant(content='尚未操作。'))
        persona = PersonaSession(client)
        tail = 'TAIL:禁止开战，活动开放时间未知'
        module.run_network_dialogue(persona, '你好', task_summary='context-' * 1550 + tail,
            research_tools=Research(), cancel_event=None, progress=None, guard=None)
        context = '\n'.join(message['content'] for message in client.requests[0]['messages']
                            if message['role'] == 'system')
        self.assertTrue(tail in context, 'tail facts were silently clipped')

    def test_disallowed_public_page_can_be_replaced_without_performing_rejected_batch(self):
        research=Research({'ok':True,'data':{'content':'公告正文','url':PAGE}})
        report=self.run_turn([
            assistant(tool('bad','web_fetch',url='https://example.com/announcement')),
            assistant(tool('good','web_fetch',url=PAGE)),assistant(content='已核对允许读取的公告。')],research)
        self.assertEqual(research.calls,[('fetch',PAGE)])
        self.assertEqual(report['response']['content'],'已核对允许读取的公告。')
        rejected=next(m for m in self.persona.messages if m['role']=='tool')
        self.assertFalse(json.loads(rejected['content'])['ok'])

    def run_turn(self, responses, research=None, **options):
        try:
            self.module = importlib.import_module('shu.network_dialogue')
        except ModuleNotFoundError as exc:
            self.fail(f'Network dialogue runner is missing: {exc.name}')
        self.client = ScriptedClient(*responses)
        self.persona = PersonaSession(self.client)
        self.research = research or Research()
        self.progress = []
        return self.module.run_network_dialogue(
            self.persona, '查一下现在游戏里的活动', task_summary='当前未执行任何游戏操作',
            research_tools=self.research, cancel_event=options.pop('cancel_event', None),
            progress=options.pop('progress', self.progress.append),
            guard=options.pop('guard', lambda: None), **options)

    def test_search_result_reaches_model_before_natural_response(self):
        result = {'ok': True, 'tool': 'search', 'data': {'results': [{
            'url': PAGE, 'title': '合成公告', 'date': '2026-11-06',
            'content': '合成测试资料，不代表实际游戏活动。'}]}}
        answer = assistant(content='测试公告尚需确认。')
        report = self.run_turn([
            assistant(tool('search', query='明日方舟 国服 当前活动'), reasoning_content='只在内存保留'),
            answer], Research(result))
        self.assertEqual(report['response'], answer)
        self.assertEqual(report['sources'], [{'title': '合成公告', 'url': PAGE, 'date': '2026-11-06'}])
        messages = self.client.requests[-1]['messages']
        recorded = next(m for m in messages if m['role'] == 'tool')
        self.assertEqual(json.loads(recorded['content'])['data'], result['data'])
        self.assertEqual(next(m for m in messages if m['role'] == 'assistant')['reasoning_content'],
                         '只在内存保留')
        self.assertEqual(sum(m['role'] == 'user' for m in self.persona.messages), 1)
        self.assertTrue(all(r['tool_choice'] == 'auto' for r in self.client.requests))
        self.assertEqual(self.research.calls, [('search', '明日方舟 国服 当前活动')])

    def test_ordinary_chat_makes_no_network_call_and_has_no_sources(self):
        answer = assistant(content='我在，今天想聊些什么？')
        report = self.run_turn([answer], routing=False)
        self.assertEqual(report, {'response': answer, 'sources': [], 'events': []})
        self.assertEqual(self.research.calls, [])
        names = {t['function']['name'] for t in self.client.requests[0]['tools']}
        self.assertEqual(names, {'web_search', 'web_fetch'})

    def test_route_is_returned_pending_after_research_for_controller(self):
        route = assistant(tool('route', 'route_user_request', intent='chat'))
        report = self.run_turn([assistant(tool('search', query='测试')), route], Research({'ok': True, 'data': {}}))
        self.assertEqual(report['response'], route)
        self.assertEqual({t['function']['name'] for t in self.client.requests[0]['tools']},
                         {'web_search', 'web_fetch', 'route_user_request'})
        self.assertEqual(sum(m['role'] == 'tool' for m in self.persona.messages), 1)
        self.persona.record_tool_result('route', {'ok': True, 'applied_by_controller': True})

    def test_entire_batch_is_validated_before_any_network_effect(self):
        bad_arguments = tool('bad', query='另一个查询')
        bad_arguments['function']['arguments'] = '{invalid'
        cases = [
            [tool('valid', query='测试'), tool('unknown', 'start_battle')],
            [tool('duplicate', query='测试'), tool('duplicate', query='测试')],
            [tool('valid', query='测试'), tool('route', 'route_user_request')],
            [tool('valid', query='测试'), bad_arguments],
            [tool('valid', query='测试'), tool('bad', query='测试', unexpected=True)],
            [tool('valid', query='测试'), tool('bad', 'web_fetch', url='https://prts.wiki/?token=private')],
            [tool('valid', query='测试'), tool('', query='测试')],
            [tool('route1', 'route_user_request'), tool('route2', 'route_user_request')],
        ]
        for calls in cases:
            with self.subTest(calls=calls):
                research = Research({'ok': True, 'data': {}}, {'ok': True, 'data': {}})
                with self.assertRaises(ProviderError):
                    self.run_turn([assistant(*calls)], research)
                self.assertEqual(research.calls, [])

    def test_chat_mode_cannot_return_an_execution_route(self):
        with self.assertRaises(ProviderError):
            self.run_turn([assistant(tool('route', 'route_user_request'))], routing=False)
        self.assertEqual(self.research.calls, [])

    def test_search_budget_rejects_whole_oversized_batch(self):
        research = Research(*[{'ok': True, 'data': {}} for _ in range(4)])
        calls = [tool(str(n), query=f'查询 {n}') for n in range(4)]
        with self.assertRaises(ProviderError):
            self.run_turn([assistant(*calls)], research)
        self.assertEqual(research.calls, [])

    def test_search_and_fetch_budgets_are_independent_across_rounds(self):
        research = Research(*[{'ok': True, 'data': {}} for _ in range(7)])
        responses = [assistant(tool(f's{n}', query=f'查询 {n}'),
                               tool(f'f{n}', 'web_fetch', url=f'{PAGE}{n}')) for n in range(3)]
        responses.append(assistant(tool('over', 'web_fetch', url=PAGE)))
        with self.assertRaises(ProviderError):
            self.run_turn(responses, research)
        self.assertEqual([name for name, _ in research.calls], ['search', 'fetch'] * 3)

    def test_model_round_limit_never_returns_fake_completed_response(self):
        research = Research({'ok': True, 'data': {}})
        with self.assertRaises(ProviderError):
            self.run_turn([assistant(tool('s', query='测试'))], research, max_steps=1)
        self.assertEqual(len(self.client.requests), 1)
        for limit in (0, -1, 13, True):
            with self.subTest(limit=limit), self.assertRaises(ProviderError):
                self.run_turn([assistant(content='不能被接受')], max_steps=limit)

    def test_cancel_before_model_has_no_effect(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            self.run_turn([assistant(content='不会展示')], cancel_event=cancel)
        self.assertEqual(self.client.requests, [])

    def test_late_network_result_is_discarded_on_cancel(self):
        cancel = threading.Event()
        def result():
            cancel.set()
            return {'ok': True, 'data': {'url': PAGE, 'title': '不会展示'}}
        with self.assertRaises(ProviderCancelled):
            self.run_turn([assistant(tool('s', query='测试')), assistant(content='不会展示')],
                          Research(result), cancel_event=cancel)
        self.assertFalse(any(m['role'] == 'tool' for m in self.persona.messages))
        self.assertFalse(any(e['stage'] == 'tool_finished' for e in self.progress))

    def test_stop_interrupts_wait_for_network_without_delivering_late_result(self):
        cancel, entered, release = threading.Event(), threading.Event(), threading.Event()
        def result():
            entered.set()
            release.wait(2)
            return {'ok': True, 'data': {'url': PAGE}}
        def stop():
            if entered.wait(1):
                cancel.set()
        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        started = time.monotonic()
        try:
            with self.assertRaises(ProviderCancelled):
                self.run_turn([assistant(tool('s', query='测试'))], Research(result), cancel_event=cancel)
            self.assertLess(time.monotonic() - started, .8)
            self.assertFalse(any(m['role'] == 'tool' for m in self.persona.messages))
        finally:
            release.set()
            stopper.join(2)

    def test_guard_invalidates_network_result_before_recording(self):
        research = Research({'ok': True, 'data': {'url': PAGE}})
        def guard():
            if research.calls:
                raise ProviderCancelled('目标版本已经变化。')
        with self.assertRaises(ProviderCancelled):
            self.run_turn([assistant(tool('s', query='测试'))], research, guard=guard)
        self.assertFalse(any(m['role'] == 'tool' for m in self.persona.messages))

    def test_tool_failure_reaches_model_without_exception_secrets(self):
        report = self.run_turn([
            assistant(tool('s', query='测试')), assistant(content='检索失败，当前活动仍未知。')],
            Research(ResearchToolError('upstream Bearer sk-private-provider-value')))
        self.assertEqual(report['sources'], [])
        returned = next(m for m in self.persona.messages if m['role'] == 'tool')
        self.assertFalse(json.loads(returned['content'])['ok'])
        self.assertNotIn('private-provider', json.dumps(self.persona.messages))
        self.assertEqual([event['stage'] for event in report['events']], ['tool_started', 'error'])
        self.assertEqual(self.progress, report['events'])

    def test_progress_and_sources_never_store_full_payload_or_hidden_reasoning(self):
        report = self.run_turn([
            assistant(tool('s', query='测试公开资料'), reasoning_content='私有思考'),
            assistant(content='资料已返回。')], Research({'ok': True, 'data': {'results': [{
                'url': PAGE, 'title': '合成结果', 'date': '2026-11-06',
                'content': '长篇资料不进事件 sk-payload-secretvalue'}]}}))
        persisted = json.dumps(report['events'], ensure_ascii=False)
        for private in ('私有思考', '长篇资料不进事件', 'sk-payload-secretvalue'):
            self.assertNotIn(private, persisted)
        self.assertEqual([e['stage'] for e in report['events']], ['tool_started', 'tool_finished'])
        self.assertEqual(report['events'][-1]['source_count'], 1)
        self.assertTrue(all('研究Agent' in e['label'] for e in report['events']))
        self.assertEqual(report['events'][-1]['tool'], 'web_search')

    def test_sources_use_nested_real_results_and_exclude_model_citations(self):
        actual = {'ok': True, 'data': {'payload': {'items': [
            {'link': PAGE, 'name': '真实结构化标题', 'published_time': '2026-11-05'},
            {'url': PAGE, 'title': '同一链接重复返回'},
            {'url': 'https://prts.wiki/?token=private', 'title': '敏感链接'},
            {'content': '正文提及 https://prts.wiki/w/NotASource'}]}}}
        report = self.run_turn([assistant(tool('s', query='测试')),
                               assistant(content='模型随意写 https://prts.wiki/w/Invented')], Research(actual))
        self.assertEqual(report['sources'], [{'title': '真实结构化标题', 'url': PAGE, 'date': '2026-11-05'}])

    def test_successful_fetch_reports_actual_url_without_inventing_title_or_date(self):
        report = self.run_turn([assistant(tool('f', 'web_fetch', url=PAGE)), assistant(content='已读取。')],
                              Research({'ok': True, 'tool': 'fetch', 'url': PAGE, 'data': {'content': '正文'}}))
        self.assertEqual(report['sources'], [{'title': '', 'url': PAGE, 'date': ''}])

    def test_failed_result_payload_does_not_count_as_a_source(self):
        report = self.run_turn([assistant(tool('s', query='测试')), assistant(content='结果仍未知。')],
                              Research({'ok': False, 'data': {'url': PAGE, 'title': '未核实'}}))
        self.assertEqual(report['sources'], [])

    def test_context_uses_current_shanghai_date_and_preserves_original_question(self):
        self.run_turn([assistant(content='尚未查询。')])
        request = self.client.requests[0]['messages']
        context = '\n'.join(m['content'] for m in request if m['role'] == 'system')
        self.assertIn(datetime.now(timezone(timedelta(hours=8))).date().isoformat(), context)
        self.assertIn('Asia/Shanghai', context)
        self.assertIn('国服', context)
        self.assertIn('游戏内', context)
        self.assertEqual(next(m for m in request if m['role'] == 'user')['content'], '查一下现在游戏里的活动')

    def test_full_source_url_is_preserved_in_progress_without_clipping(self):
        url = PAGE + '/' + 'public-path-' * 40
        report = self.run_turn([assistant(tool('f', 'web_fetch', url=url)), assistant(content='已读取。')],
                              Research({'ok': True, 'data': {'url': url, 'title': '公开资料'}}))
        self.assertEqual(report['sources'][0]['url'], url)
        self.assertEqual(report['events'][0]['url'], url)
        self.assertEqual(report['events'][1]['url'], url)

    def test_later_failure_preserves_earlier_real_sources(self):
        report = self.run_turn([
            assistant(tool('search', query='测试')),
            assistant(tool('fetch', 'web_fetch', url=PAGE)),
            assistant(content='只找到搜索摘要，正文读取失败。')], Research(
                {'ok': True, 'data': {'results': [{'url': PAGE, 'title': '真实摘要'}]}},
                RuntimeError('opaque-provider-secret')))
        self.assertEqual(report['sources'], [{'url': PAGE, 'title': '真实摘要', 'date': ''}])
        self.assertEqual([e['status'] for e in report['events']], ['running', 'completed', 'running', 'failed'])

    def test_reused_call_id_cannot_run_network_twice(self):
        research = Research({'ok': True, 'data': {}}, {'ok': True, 'data': {}})
        with self.assertRaises(ProviderError):
            self.run_turn([assistant(tool('same', query='首次')), assistant(tool('same', query='第二次'))], research)
        self.assertEqual(research.calls, [('search', '首次')])

    def test_missing_tool_result_shape_is_reported_as_failure(self):
        report = self.run_turn([assistant(tool('s', query='测试')), assistant(content='查询结果无效。')],
                              Research({'data': {'url': PAGE, 'title': '不是已核实结果'}}))
        self.assertEqual(report['sources'], [])
        self.assertEqual(report['events'][-1]['status'], 'failed')

    def test_empty_model_response_is_a_protocol_error(self):
        with self.assertRaises(ProviderError):
            self.run_turn([assistant(content='  ')])

    def test_model_sees_remaining_research_budget_after_each_real_call(self):
        self.run_turn([assistant(tool('s', query='测试')), assistant(content='结果为空。')],
                      Research({'ok': True, 'data': {}}))
        context = '\n'.join(m['content'] for m in self.client.requests[1]['messages'] if m['role'] == 'system')
        self.assertIn('web_search 剩余 2 次', context)
        self.assertIn('web_fetch 剩余 3 次', context)

    def test_explicit_credentials_are_rejected_before_any_outbound_request(self):
        calls = [tool('bad', query='检索 sk-private-secretvalue'),
                 tool('bad', query='查 Bearer opaque-credential'),
                 tool('bad', 'web_fetch', url=PAGE + '/sk-private-secretvalue')]
        for forbidden in calls:
            with self.subTest(forbidden=forbidden):
                research = Research({'ok': True, 'data': {}})
                with self.assertRaises(ProviderError):
                    self.run_turn([assistant(tool('valid', query='公开资料'), forbidden)], research)
                self.assertEqual(research.calls, [])

    def test_http_failure_code_reaches_model_without_remote_secret_text(self):
        self.run_turn([assistant(tool('s', query='公开资料')), assistant(content='检索服务受限，当前情况未知。')],
                      Research(ResearchToolError('资料请求失败（HTTP 402）：opaque-provider-secret')))
        result = json.loads(next(m['content'] for m in self.persona.messages if m['role'] == 'tool'))
        self.assertEqual(result['http_status'], 402)
        self.assertEqual(result['error_code'], 'payment_required')
        self.assertNotIn('opaque-provider-secret', json.dumps(self.persona.messages))
        self.assertEqual(self.progress[-1]['http_status'], 402)

    def test_partial_result_preserves_real_catalog_and_safe_web_failure_code(self):
        report = self.run_turn([assistant(tool('s', query='TEST-1 作业')),
                               assistant(content='网页限流，只取得公开目录。')], Research({
            'ok': False, 'web_ok': False, 'web_error': '资料请求失败（HTTP 429）：opaque-provider-secret',
            'data': {}, 'catalog': {'source_url': PAGE, 'title': '实际公开目录', 'candidates': []}}))
        result = json.loads(next(m['content'] for m in self.persona.messages if m['role'] == 'tool'))
        self.assertEqual(result['http_status'], 429)
        self.assertEqual(result['error_code'], 'rate_limited')
        self.assertEqual(report['sources'], [{'url': PAGE, 'title': '实际公开目录', 'date': ''}])
        self.assertNotIn('opaque-provider-secret', json.dumps(self.persona.messages))


if __name__ == '__main__':
    unittest.main()

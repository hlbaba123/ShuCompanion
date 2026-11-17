"""Offline research safety contracts; never opens a paid API or a game."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from shu.providers import ProviderConfig
from shu.copilot_catalog import CopilotCatalogError
from shu.research_tools import (
    FETCH_LIMIT, MAX_BYTES, SEARCH_LIMIT, ResearchToolError, ResearchTools,
    _NoRedirect,
)


TEST_KEY = 'sk-offline-test-credential'
DATA_URL = 'https://prts.maa.plus/copilot/get/73921'
PAGE_URL = 'https://prts.plus/?op=73921'


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


class FakeTransport:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def __call__(self, req, **kwargs):
        self.calls.append((req, kwargs))
        if not self.values:
            raise AssertionError('Unexpected network request')
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value if isinstance(value, bytes) else encoded(value)


class ResearchToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.config = ProviderConfig(api_key=TEST_KEY, base_url='https://api.deepseek.com')
        self.config.resolve_key = Mock(return_value=TEST_KEY)

    def make(self, *values):
        transport = FakeTransport(*values)
        return ResearchTools(self.config, self.path, transport=transport,
                             catalog_query=Mock(return_value={'candidates': []})), transport

    def test_unused_research_tools_do_not_enumerate_windows_tls_certificates(self):
        with patch('shu.research_tools.ssl.create_default_context',
                   side_effect=AssertionError('Unused research must not prepare TLS')):
            tools = ResearchTools(self.config, self.path)
            self.assertEqual(tools.stats['search'], 0)
            self.config.resolve_key.assert_not_called()

    def test_default_transport_is_prepared_only_for_first_request_and_reused(self):
        network = FakeTransport({'results': []}, {'results': []})
        with patch('shu.research_tools._HTTPTransport', return_value=network) as factory:
            tools = ResearchTools(self.config, self.path)
            factory.assert_not_called()
            self.assertTrue(tools.search('first public query')['ok'])
            factory.assert_called_once_with()
            self.assertTrue(tools.search('second public query')['ok'])
            factory.assert_called_once_with()
            self.assertEqual(len(network.calls), 2)

    def test_fresh_disk_cache_does_not_initialize_default_tls_transport(self):
        tools, _ = self.make({'results': []})
        tools.search('cached public query')
        with patch('shu.research_tools._HTTPTransport',
                   side_effect=AssertionError('Fresh cache must not prepare TLS')):
            other = ResearchTools(self.config, self.path)
            self.assertTrue(other.search('cached public query')['cached'])

    def test_search_fixed_endpoint_contract_and_memory_cache(self):
        tools, network = self.make({'results': [{'url': DATA_URL, 'title': '实际搜索结果'}]})
        result = tools.search(' TL-2 黍 作业 ')
        req, kwargs = network.calls[0]
        self.assertEqual(req.full_url, 'https://api.deepseek.com/v1/tools/search')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Authorization'), 'Bearer ' + TEST_KEY)
        self.assertEqual(json.loads(req.data), {'query': 'TL-2 黍 作业', 'count': 5, 'need_content': True})
        self.assertEqual(kwargs, {'timeout': 30, 'max_bytes': MAX_BYTES})
        self.assertTrue(result['untrusted_content'])
        self.assertFalse(result['content_is_instruction'])
        self.assertIn(DATA_URL, tools.discovered_copilot_urls)
        result['data'].clear()  # callers cannot mutate the cache
        cached = tools.search('TL-2 黍 作业')
        self.assertTrue(cached['cached'])
        self.assertTrue(cached['data'])
        self.assertEqual(len(network.calls), 1)
        self.config.resolve_key.assert_called_once_with()

    def test_disk_cache_does_not_resolve_key_or_consume_budget(self):
        tools, _ = self.make({'results': [{'url': PAGE_URL}]})
        tools.search('关卡作业')
        config = ProviderConfig()
        config.resolve_key = Mock(side_effect=AssertionError('No key needed'))
        transport = FakeTransport()
        other = ResearchTools(config, self.path, transport=transport)
        result = other.search('关卡作业')
        self.assertTrue(result['cached'])
        self.assertEqual(other.stats['search'], 0)
        self.assertIn(DATA_URL, other.discovered_copilot_urls)
        self.assertEqual(transport.calls, [])

    def test_search_memory_cache_expires_at_five_minutes(self):
        tools, _ = self.make({'results': [{'title': 'old announcement'}]},
                             {'results': [{'title': 'new announcement'}]})
        with patch('time.time', return_value=1000):
            first = tools.search('当前游戏内活动')
        with patch('time.time', return_value=1299):
            cached = tools.search('当前游戏内活动')
        with patch('time.time', return_value=1300):
            fresh = tools.search('当前游戏内活动')
        self.assertFalse(first['cached'])
        self.assertTrue(cached['cached'])
        self.assertFalse(fresh['cached'])
        self.assertEqual(fresh['data']['results'][0]['title'], 'new announcement')
        self.assertEqual(tools.stats['search'], 2)

    def test_fetch_disk_cache_expires_at_fifteen_minutes(self):
        url = 'https://prts.wiki/w/活动'
        tools, _ = self.make({'content': 'old body'})
        with patch('time.time', return_value=1000):
            tools.fetch(url)
        other, _ = self.make({'content': 'new body'})
        with patch('time.time', return_value=1899):
            cached = other.fetch(url)
        with patch('time.time', return_value=1900):
            fresh = other.fetch(url)
        self.assertTrue(cached['cached'])
        self.assertFalse(fresh['cached'])
        self.assertEqual(fresh['data']['content'], 'new body')

    def test_missing_or_invalid_cache_timestamps_force_new_public_research(self):
        for number, timestamp in enumerate((None, True, '1000', -1, 1001,
                                             float('nan'), float('inf'), 10 ** 1000)):
            with self.subTest(timestamp=timestamp):
                tools, _ = self.make({'results': [{'title': 'stale answer'}]})
                tools.cache_dir = self.path / str(number)
                with patch('time.time', return_value=1000):
                    tools.search('当前公告')
                cache_path = tools._cache_path('search:当前公告')
                record = json.loads(cache_path.read_text(encoding='utf-8'))
                if timestamp is None:
                    record.pop('written_at', None)
                else:
                    record['written_at'] = timestamp
                cache_path.write_text(json.dumps(record), encoding='utf-8')
                other, _ = self.make({'results': [{'title': 'fresh answer'}]})
                other.cache_dir = tools.cache_dir
                with patch('time.time', return_value=1000):
                    result = other.search('当前公告')
                self.assertFalse(result['cached'])
                self.assertEqual(result['data']['results'][0]['title'], 'fresh answer')

    def test_search_preserves_all_five_source_cards_when_first_body_is_long(self):
        cards = [{'content': '长正文' * 9000, 'irrelevant_blob': 'x' * 9000,
                  'title': f'公告{number}', 'url': f'https://ak.hypergryph.com/news/{number}',
                  'date': '2026-11-06', 'authority': 1, 'rerank_score': .9,
                  'snippet': f'阶段开放{number}'} for number in range(5)]
        for number, wrapper in enumerate((cards, {'results': cards}, {'data': {'results': cards}})):
            with self.subTest(wrapper=number):
                tools, _ = self.make(wrapper)
                tools.cache_dir = self.path / f'cards-{number}'
                result = tools.search('游戏内活动公告')
                data = result['data']
                actual = data if isinstance(data, list) else data.get('results', data.get('data', {}).get('results'))
                self.assertEqual(len(actual), 5)
                for item, original in zip(actual, cards):
                    for key in ('title', 'url', 'date', 'authority', 'rerank_score', 'snippet'):
                        self.assertEqual(item[key], original[key])
                    self.assertLessEqual(len(item['content']), 1000)
                cached = tools.search('游戏内活动公告')
                self.assertEqual(cached['data'], data)
                self.assertLess(len(json.dumps(result, ensure_ascii=False)), 16000)

    def test_fetch_accepts_only_exact_new_official_game_hosts(self):
        tools, _ = self.make({'content': 'official game announcement'},
                             {'content': 'official company announcement'})
        for host in ('ak.hypergryph.com', 'www.hypergryph.com'):
            result = tools.fetch(f'https://{host}/news/test')
            self.assertTrue(result['ok'])
        for host in ('hypergryph.com', 'other.hypergryph.com', 'ak.hypergryph.com.evil.invalid'):
            with self.subTest(host=host), self.assertRaises(ResearchToolError):
                tools.fetch(f'https://{host}/news/test')

    def test_fetch_allowlist_and_payload(self):
        tools, network = self.make({'text': 'PRTS 实际页面'})
        url = 'https://prts.wiki/w/public-reference'
        result = tools.fetch(url)
        req, _ = network.calls[0]
        self.assertEqual(req.full_url, 'https://api.deepseek.com/v1/tools/fetch')
        self.assertEqual(json.loads(req.data), {'url': url})
        self.assertEqual(result['discovered_copilots'], [])
        self.assertTrue(tools.fetch(url)['cached'])
        self.assertEqual(tools.stats['fetch'], 1)

    def test_fetch_rejects_private_or_ambiguous_urls_before_key_or_network(self):
        invalid = [
            'http://prts.plus/?op=12', 'https://localhost/', 'https://127.0.0.1/',
            'https://[::1]/', 'https://prts.plus.evil.invalid/',
            'https://user:password@prts.plus/', 'https://prts.plus:443/',
            'https://prts.plus:/', 'https://prts.plus./', 'https://prts.plus/#fragment',
            'https://prts.plus/?api_key=value', 'https://prts.plus/?access%5Ftoken=value',
            'https://prts.plus/?sessionId=value', 'https://prts.plus/?q=sk-private-secret',
            'https://prts.plus/?redirect=https%3A%2F%2Flocalhost',
            'https://prts.plus\\@evil.invalid/', 'https://prts.plus/\n',
        ]
        tools, network = self.make()
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ResearchToolError):
                tools.fetch(url)
        self.assertEqual(network.calls, [])
        self.config.resolve_key.assert_not_called()

    def test_public_catalog_keyword_parameter_is_not_a_private_key(self):
        tools, network = self.make({'text': 'public query'})
        url = 'https://prts.maa.plus/copilot/query?level_keyword=AB-9'
        self.assertTrue(tools.fetch(url)['ok'])
        self.assertEqual(json.loads(network.calls[0][0].data), {'url': url})

    def test_search_and_fetch_have_separate_strict_budgets(self):
        tools, network = self.make(*([{'text': 'ok'}] * (SEARCH_LIMIT + FETCH_LIMIT)))
        for number in range(SEARCH_LIMIT):
            tools.search(f'关卡 {number}')
        for number in range(FETCH_LIMIT):
            tools.fetch(f'https://prts.wiki/?page={number}')
        self.assertTrue(tools.search('关卡 0')['cached'])
        with self.assertRaises(ResearchToolError):
            tools.search('预算之外')
        with self.assertRaises(ResearchToolError):
            tools.fetch('https://prts.wiki/?page=extra')
        self.assertEqual(len(network.calls), SEARCH_LIMIT + FETCH_LIMIT)

    def test_failures_spend_budget_and_never_echo_key(self):
        values = [RuntimeError('Bearer ' + TEST_KEY), b'not-json', b'x' * (MAX_BYTES + 1),
                  {'error': {'message': TEST_KEY}}, b'{"x":NaN}']
        tools, _ = self.make(*values)
        for number in range(len(values)):
            with self.assertRaises(ResearchToolError) as caught:
                tools.search(f'失败 {number}')
            self.assertNotIn(TEST_KEY, str(caught.exception))
        self.assertEqual(tools.stats['search'], len(values))

    def test_http_error_and_redirect_errors_do_not_include_remote_text(self):
        tools, _ = self.make(HTTPError('https://example.invalid', 302, TEST_KEY, {}, None))
        with self.assertRaises(ResearchToolError) as caught:
            tools.search('作业')
        self.assertIn('302', str(caught.exception))
        self.assertNotIn(TEST_KEY, str(caught.exception))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_all_untrusted_strings_share_a_budget(self):
        tools, _ = self.make({'long': ['黍' * 9000] * 50})
        result = tools.search('资料')
        def string_count(value):
            if isinstance(value, str):
                return len(value)
            if isinstance(value, dict):
                return sum(len(key) + string_count(child) for key, child in value.items())
            if isinstance(value, list):
                return sum(map(string_count, value))
            return 0
        self.assertLessEqual(string_count(result), 16000)

    def test_download_requires_discovery_and_does_not_leak_authorization(self):
        copilot = {'stage_name': 'a_test_stage', 'actions': [{'type': 'Deploy', 'name': '黍'}],
                   'opers': [{'name': '黍', 'skill': 3}], 'minimum_required': 'v5.0.0',
                   'arbitrary': '完整保存，不仅摘要'}
        tools, network = self.make({'results': [{'url': DATA_URL}]},
                                   {'status_code': 200, 'data': {'content': json.dumps(copilot)}})
        with self.assertRaises(ResearchToolError):
            tools.download_copilot(DATA_URL)
        self.assertEqual(network.calls, [])
        tools.search('模型自行寻找作业')
        result = tools.download_copilot(DATA_URL)
        req, _ = network.calls[-1]
        self.assertEqual(req.full_url, DATA_URL)
        self.assertEqual(req.get_method(), 'GET')
        self.assertIsNone(req.get_header('Authorization'))
        path = Path(result['path'])
        self.assertEqual(path.parent, self.path / 'copilots')
        self.assertEqual(json.loads(path.read_bytes()), copilot)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), result['sha256'])
        self.assertEqual(result['action_count'], 1)
        self.assertEqual(result['operator_names'], ['黍'])
        self.assertTrue(result['no_raw_click'])
        self.assertFalse(result['executed'])
        self.assertTrue(tools.download_copilot(DATA_URL)['cached'])
        self.assertEqual(len(network.calls), 2)

    def test_derived_endpoint_records_actual_page_provenance(self):
        copilot = {'stage_name': 'x', 'actions': [{'type': 'RawClick', 'location': [1, 2]}]}
        tools, network = self.make({'results': [{'url': PAGE_URL}]}, copilot)
        result = tools.search('模型发现页面')
        self.assertEqual(result['discovered_copilots'][0]['url'], DATA_URL)
        downloaded = tools.download_copilot(PAGE_URL)
        self.assertEqual(network.calls[-1][0].full_url, DATA_URL)
        self.assertEqual(downloaded['derived_source'], PAGE_URL)
        self.assertFalse(downloaded['no_raw_click'])

    def test_undiscovered_id_and_alias_remain_rejected(self):
        tools, network = self.make({'url': DATA_URL})
        tools.search('作业')
        for url in ('https://prts.maa.plus/copilot/get/73922', PAGE_URL,
                    DATA_URL + '?q=x', DATA_URL + '/more'):
            with self.subTest(url=url), self.assertRaises(ResearchToolError):
                tools.download_copilot(url)
        self.assertEqual(len(network.calls), 1)

    def test_clipped_urls_cannot_invent_ids_on_cache_hits(self):
        full = 'https://prts.maa.plus/copilot/get/123456789012'
        prefix = 'x ' * 1980
        tools, _ = self.make({'text': prefix + full})
        tools.search('截断资料')
        self.assertEqual(tools.discovered_copilot_urls, set())
        tools.search('截断资料')
        self.assertEqual(tools.discovered_copilot_urls, set())

    def test_invalid_copilot_not_saved(self):
        invalid = [{'stage_name': 'x'}, {'actions': []}, {'stage_name': 'x', 'actions': ['bad']},
                   {'data': {'content': 'not-json'}}]
        tools, _ = self.make({'url': DATA_URL}, *invalid)
        tools.search('模型发现')
        for _ in invalid:
            with self.assertRaises(ResearchToolError):
                tools.download_copilot(DATA_URL)
        self.assertFalse((self.path / 'copilots').exists())

    def test_query_official_128_character_limit(self):
        tools, network = self.make({'text': 'ok'})
        tools.search('黍' * 128)
        with self.assertRaises(ResearchToolError):
            tools.search('黍' * 129)
        self.assertEqual(len(network.calls), 1)

    def test_download_preserves_real_roster_requirements_and_never_claims_executable(self):
        operators = [{'name': '黍', 'skill': 3,
                      'requirements': {'elite': 2, 'level': 90, 'skill_level': 10}}]
        groups = [{'name': '先锋', 'opers': [{'name': '桃金娘', 'skill': 1,
                                           'requirements': {'elite': 2, 'level': 20}}]}]
        for action_type in ('Click', 'RawClick', 'Swipe'):
            with self.subTest(action_type=action_type):
                tools, _ = self.make({'url': DATA_URL}, {'stage_name': 'x', 'opers': operators,
                                       'groups': groups, 'actions': [{'type': action_type}]})
                tools.cache_dir = self.path / action_type
                tools.search('发现来源')
                result = tools.download_copilot(DATA_URL)
                self.assertEqual(result['opers'], operators)
                self.assertEqual(result['groups'], groups)
                self.assertFalse(result['no_raw_click'])
                self.assertFalse(result['executability_report']['can_auto_execute'])
                self.assertIn(action_type, result['executability_report']['unsupported_action_types'])

    def test_catalog_generic_stage_discovery_cache_and_independent_budget(self):
        tools, network = self.make(*([{'text': 'web'}] * 5))
        catalog = Mock(return_value={'source': 'https://prts.maa.plus/copilot/query?level_keyword=AB-9',
                                     'candidates': [{'source': DATA_URL}]})
        tools.catalog_query = catalog
        first = tools.search('AB-9 MAA 作业')
        self.assertEqual(first['catalog']['candidates'][0]['source'], DATA_URL)
        self.assertIn(DATA_URL, tools.discovered_copilot_urls)
        self.assertEqual(tools.discovery_sources[DATA_URL][0]['operation'], 'catalog')
        tools.search('ab-9 copilot')
        tools.search('XY-12 prts')
        limited = tools.search('Z-3 作业')
        ambiguous = tools.search('AB-9 CD-8 作业')
        self.assertIn('error', limited['catalog'])
        self.assertNotIn('catalog', ambiguous)
        self.assertEqual(catalog.call_count, 2)
        self.assertEqual(tools.stats['catalog'], 2)
        self.assertEqual(len(network.calls), 5)
        tools.search('AB-9 MAA 作业')
        self.assertEqual(catalog.call_count, 2)

    def test_catalog_error_is_bounded_and_does_not_fail_web_search(self):
        tools, _ = self.make({'text': 'web succeeded'})
        tools.catalog_query = Mock(side_effect=RuntimeError(TEST_KEY))
        result = tools.search('QQ-7 作业')
        self.assertTrue(result['ok'])
        self.assertIn('error', result['catalog'])
        self.assertNotIn(TEST_KEY, json.dumps(result))

    def test_web_failure_still_returns_and_discovers_real_catalog_candidates(self):
        tools, network = self.make(HTTPError('https://example.invalid', 404, TEST_KEY, {}, None))
        source = 'https://prts.maa.plus/copilot/query?level_keyword=QQ-7'
        tools.catalog_query = Mock(return_value={'source': source,
                                                'candidates': [{'source': DATA_URL}]})
        result = tools.search('QQ-7 MAA 作业')
        self.assertFalse(result['ok'])
        self.assertFalse(result['web_ok'])
        self.assertIn('404', result['web_error'])
        self.assertEqual(result['data'], {})
        self.assertEqual(result['catalog']['source'], source)
        self.assertIn(DATA_URL, tools.discovered_copilot_urls)
        self.assertEqual(tools.discovery_sources[DATA_URL], [
            {'operation': 'catalog', 'source': source,
             'discovered_url': DATA_URL, 'derived_source': None}])
        self.assertNotIn(TEST_KEY, json.dumps(result))
        self.assertEqual(len(network.calls), 1)
        self.assertEqual(tools.stats['catalog'], 1)

    def test_catalog_budget_and_cache_survive_exhausted_web_budget(self):
        tools, network = self.make(*([RuntimeError(TEST_KEY)] * SEARCH_LIMIT))
        tools.catalog_query = Mock(return_value={'source': 'https://prts.maa.plus/copilot/query',
                                                'candidates': [{'source': DATA_URL}]})
        for number in range(SEARCH_LIMIT):
            with self.assertRaises(ResearchToolError):
                tools.search(f'普通检索 {number}')
        first = tools.search('AB-9 作业')
        cached = tools.search('ab-9 copilot')
        second = tools.search('XY-12 prts')
        self.assertEqual(first['catalog'], cached['catalog'])
        self.assertIn('预算', second['web_error'])
        with self.assertRaises(ResearchToolError) as caught:
            tools.search('Z-3 作业')
        self.assertIn('search', str(caught.exception))
        self.assertEqual(tools.catalog_query.call_count, 2)
        self.assertEqual(tools.stats['catalog'], 2)
        self.assertEqual(tools.stats['search'], SEARCH_LIMIT)
        self.assertEqual(len(network.calls), SEARCH_LIMIT)

    def test_both_research_sources_failing_preserves_original_web_error(self):
        tools, _ = self.make(HTTPError('https://example.invalid', 503, TEST_KEY, {}, None))
        tools.catalog_query = Mock(side_effect=RuntimeError(TEST_KEY))
        with self.assertRaises(ResearchToolError) as caught:
            tools.search('QQ-7 作业')
        self.assertIn('503', str(caught.exception))
        self.assertNotIn(TEST_KEY, str(caught.exception))
        tools.catalog_query.assert_called_once_with('QQ-7')
        self.assertEqual(tools.discovered_copilot_urls, set())

    def test_ambiguous_stages_do_not_query_catalog_after_web_failure(self):
        tools, _ = self.make(RuntimeError(TEST_KEY))
        with self.assertRaises(ResearchToolError):
            tools.search('AB-9 CD-8 作业')
        tools.catalog_query.assert_not_called()

    def test_combined_web_and_catalog_strings_remain_within_total_budget(self):
        urls = ['https://prts.plus/?op=' + str(123456789000 + number) for number in range(20)]
        tools, _ = self.make({'urls': urls, 'text': '黍' * 9000})
        tools.catalog_query = Mock(return_value={
            'source': 'https://prts.maa.plus/copilot/query?level_keyword=AB-9',
            'candidates': [{'source': url} for url in urls], 'text': '作业' * 9000})
        result = tools.search('AB-9 作业')
        def string_count(value):
            if isinstance(value, str):
                return len(value)
            if isinstance(value, dict):
                return sum(len(key) + string_count(child) for key, child in value.items())
            if isinstance(value, list):
                return sum(map(string_count, value))
            return 0
        self.assertTrue(result['catalog']['candidates'])
        self.assertLessEqual(string_count(result), 16000)

    def test_catalog_failures_are_retryable_within_budget_and_expose_safe_reason(self):
        tools, network = self.make({'text': 'web'})
        tools.catalog_query = Mock(side_effect=[
            CopilotCatalogError('private detail', reason='http', http_status=400),
            {'source': 'https://prts.maa.plus/copilot/query',
             'candidates': [{'source': DATA_URL}]}])
        first = tools.search('AB-9 作业')
        self.assertIn('HTTP 400', first['catalog']['error'])
        self.assertNotIn('private', json.dumps(first))
        self.assertNotIn('AB-9', tools._catalog_cache)
        second = tools.search('AB-9 作业')
        self.assertIn(DATA_URL, tools.discovered_copilot_urls)
        self.assertEqual(second['catalog']['candidates'][0]['source'], DATA_URL)
        self.assertEqual(tools.stats['catalog'], 2)
        tools.search('AB-9 作业')
        self.assertEqual(tools.catalog_query.call_count, 2)
        self.assertEqual(len(network.calls), 1)

    def test_repeated_catalog_failure_stops_at_two_attempts(self):
        tools, _ = self.make({'text': 'web'})
        tools.catalog_query = Mock(side_effect=CopilotCatalogError('private', reason='timeout'))
        for _ in range(2):
            result = tools.search('AB-9 作业')
            self.assertIn('超时', result['catalog']['error'])
        limited = tools.search('AB-9 作业')
        self.assertIn('预算', limited['catalog']['error'])
        self.assertEqual(tools.catalog_query.call_count, 2)
        self.assertEqual(tools.stats['catalog'], 2)

    def test_unsupported_catalog_filter_never_registers_candidates_or_caches_success(self):
        tools, _ = self.make({'text': 'web'})
        tools.catalog_query = Mock(side_effect=CopilotCatalogError('untrusted body', reason='unsupported_filter'))
        result = tools.search('AB-9 作业')
        self.assertEqual(result['catalog']['error_code'], 'unsupported_filter')
        self.assertNotIn('candidates', result['catalog'])
        self.assertNotIn('AB-9', tools._catalog_cache)
        self.assertEqual(tools.discovered_copilot_urls, set())

    def test_unverified_real_catalog_sources_are_available_for_read_only_download(self):
        tools, network = self.make({'text': 'web'},
                                   {'stage_name': 'internal_alias', 'actions': [{'type': 'Deploy'}]})
        tools.catalog_query = Mock(return_value={
            'source': 'https://prts.maa.plus/copilot/query?levelKeyword=AB-9',
            'candidates': [], 'unverified_candidates': [{'source': DATA_URL, 'stage_name': 'internal_alias'}],
            'filter_status': 'unsupported_filter', 'stage_match_verified': False})
        result = tools.search('AB-9 作业')
        self.assertFalse(result['catalog']['stage_match_verified'])
        self.assertEqual(result['catalog']['filter_status'], 'unsupported_filter')
        self.assertIn(DATA_URL, tools.discovered_copilot_urls)
        downloaded = tools.download_copilot(DATA_URL)
        self.assertFalse(downloaded['executed'])
        self.assertFalse(downloaded['executability_report']['can_auto_execute'])
        self.assertIsNone(network.calls[-1][0].get_header('Authorization'))

    def test_explicit_catalog_action_shares_search_budget_cache_and_provenance(self):
        tools, network = self.make({'text': 'web'}, {'text': 'web'})
        tools.catalog_query = Mock(return_value={
            'source': 'https://prts.maa.plus/copilot/query?levelKeyword=AB-9',
            'candidates': [{'source': DATA_URL}]})
        direct = tools.catalog('AB-9')
        self.assertEqual(direct['catalog']['candidates'][0]['source'], DATA_URL)
        self.assertIn(DATA_URL, tools.discovered_copilot_urls)
        self.assertEqual(tools.discovery_sources[DATA_URL][0]['operation'], 'catalog')
        tools.search('AB-9 作业')
        tools.catalog('从资料学到的内部别名')
        limited = tools.search('XY-12 作业')
        self.assertIn('预算', limited['catalog']['error'])
        self.assertEqual(tools.catalog_query.call_count, 2)
        self.assertEqual(tools.stats['catalog'], 2)
        self.assertEqual(len(network.calls), 2)
        self.assertTrue(tools.catalog('AB-9')['cached'])

    def test_explicit_catalog_action_retains_unverified_sources_without_success_claim(self):
        tools, network = self.make()
        tools.catalog_query = Mock(return_value={
            'source': 'https://prts.maa.plus/copilot/query?levelKeyword=AB-9',
            'candidates': [], 'unverified_candidates': [{'source': DATA_URL}],
            'filter_status': 'unsupported_filter', 'stage_match_verified': False})
        result = tools.catalog('AB-9')
        self.assertFalse(result['ok'])
        self.assertEqual(result['catalog']['filter_status'], 'unsupported_filter')
        self.assertIn(DATA_URL, tools.discovered_copilot_urls)
        self.assertEqual(network.calls, [])
        self.config.resolve_key.assert_not_called()

    def test_levels_uses_injected_public_catalog_and_keeps_metadata_without_credentials(self):
        network = FakeTransport()
        query = Mock(return_value={
            'levels': [{'stage_id': 'synthetic_internal', 'level_id': 'level_synthetic',
                        'code': 'AB-9', 'name': 'Synthetic stage', 'event': 'Synthetic event'}],
            'source': 'https://prts.maa.plus/arknights/level', 'cached': True,
            'untrusted_content': False, 'content_is_instruction': True,
            'detail': '黍' * 15000})
        tools = ResearchTools(self.config, self.path, transport=network, level_query=query)
        result = tools.levels(' AB-9 ')
        query.assert_called_once_with('AB-9', self.path)
        self.assertTrue(result['ok'])
        self.assertEqual(result['tool'], 'levels')
        self.assertEqual(result['data']['levels'][0]['stage_id'], 'synthetic_internal')
        self.assertEqual(result['data']['source'], 'https://prts.maa.plus/arknights/level')
        self.assertTrue(result['data']['cached'])
        self.assertTrue(result['untrusted_content'])
        self.assertFalse(result['content_is_instruction'])
        self.assertTrue(result['data']['untrusted_content'])
        self.assertFalse(result['data']['content_is_instruction'])
        self.assertLessEqual(len(json.dumps(result['data'], ensure_ascii=False)), 6000)
        self.assertEqual(tools.discovered_copilot_urls, set())
        self.assertEqual(network.calls, [])
        self.config.resolve_key.assert_not_called()

    def test_levels_enforces_two_call_budget_and_redacts_failures(self):
        query = Mock(side_effect=[RuntimeError(TEST_KEY), {'levels': []}])
        tools = ResearchTools(self.config, self.path, transport=FakeTransport(), level_query=query)
        for invalid in ('', 'x' * 129, 'stage\nquery', None):
            with self.assertRaises(ResearchToolError):
                tools.levels(invalid)
        self.assertEqual(tools.stats['levels'], 0)
        with self.assertRaises(ResearchToolError) as caught:
            tools.levels('AB-9')
        self.assertNotIn(TEST_KEY, str(caught.exception))
        self.assertTrue(tools.levels('AB-9')['ok'])
        with self.assertRaisesRegex(ResearchToolError, '预算'):
            tools.levels('AB-9')
        self.assertEqual(tools.stats['levels'], 2)
        self.assertEqual(query.call_count, 2)
        self.config.resolve_key.assert_not_called()


if __name__ == '__main__':
    unittest.main()

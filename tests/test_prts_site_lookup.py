"""PRTS site queries use real metadata IDs, never search snippets as stage proof."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from shu.copilot_catalog import query_copilots
from shu.providers import ProviderConfig
from shu.research_tools import ResearchTools, ResearchToolError


def envelope(stage='synthetic_stage_09', title='AB-9 sample', identifier=42):
    return {'status_code': 200, 'data': {'has_next': True, 'total': 7, 'data': [{
        'id': identifier, 'type': 'PRTS', 'content': json.dumps({
            'stage_name': stage, 'doc': {'title': title}, 'opers': [],
            'actions': [{'type': 'SpeedUp'}],
        })}]}}


class SiteLookupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = ProviderConfig()
        self.config.resolve_key = Mock(side_effect=AssertionError('No credentials for site queries'))
        self.levels = {'source': 'https://prts.maa.plus/arknights/level', 'raw_sha256': 'a' * 64,
                       'levels': [{'code': 'AB-9', 'stage_id': 'synthetic_stage_09',
                                   'level_id': 'activities/synthetic/level_09', 'match': 'exact_code_or_id'}]}

    def test_default_catalog_resolves_display_code_before_wire_request(self):
        lookup = Mock(return_value=self.levels)
        tools = ResearchTools(self.config, self.directory, level_query=lookup)
        calls = []
        def network(req, **kwargs):
            calls.append(req)
            return json.dumps(envelope()).encode()
        with patch('shu.copilot_catalog._http_transport', network):
            result = tools.catalog('AB-9')
        self.assertEqual(parse_qs(urlsplit(calls[0].full_url).query)['levelKeyword'], ['synthetic_stage_09'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['stage_resolution']['raw_sha256'], 'a' * 64)
        item = result['catalog']['candidates'][0]
        self.assertEqual(item['page_url'], 'https://prts.plus/?op=42')
        self.assertEqual(item['mystery_code'], 'prts://42')
        self.assertIn(item['source'], tools.discovered_copilot_urls)
        self.assertEqual(result['catalog']['filter_status'], 'exact_stage_id')
        self.assertTrue(result['catalog']['stage_match_verified'])
        self.config.resolve_key.assert_not_called()

    def test_missing_exact_code_does_not_issue_unfiltered_site_request(self):
        tools = ResearchTools(self.config, self.directory, level_query=Mock(return_value={'levels': []}))
        with patch('shu.copilot_catalog._http_transport', side_effect=AssertionError('Unresolved code')):
            result = tools.catalog('AB-9')
        self.assertFalse(result['ok'])
        self.assertIn('精确', result['catalog']['error'])

    def test_existing_level_discovery_is_reused_and_page_two_keeps_resolution(self):
        lookup = Mock(return_value=self.levels)
        tools = ResearchTools(self.config, self.directory, level_query=lookup)
        tools.levels('AB-9')
        calls = []
        def network(req, **kwargs):
            calls.append(req)
            return json.dumps(envelope()).encode()
        with patch('shu.copilot_catalog._http_transport', network):
            first = tools.catalog('AB-9')
            second = tools.catalog('AB-9', page=2)
            cached = tools.catalog('AB-9')
        lookup.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertEqual(tools.stats['levels'], 1)
        self.assertTrue(cached['cached'])
        self.assertEqual(first['stage_resolution'], second['stage_resolution'])
        self.assertEqual(parse_qs(urlsplit(calls[1].full_url).query)['page'], ['2'])

    def test_wrong_stage_with_target_in_title_is_not_success(self):
        tools = ResearchTools(self.config, self.directory, level_query=Mock(return_value=self.levels))
        with patch('shu.copilot_catalog._http_transport', return_value=json.dumps(
                envelope(stage='unrelated', title='AB-9 multi-stage guide')).encode()):
            result = tools.catalog('AB-9')
        self.assertFalse(result['ok'])
        self.assertEqual(result['catalog']['candidates'], [])
        self.assertFalse(result['catalog']['stage_match_verified'])

    def test_internal_stage_query_rejects_description_only_false_positive(self):
        calls = []
        def network(req, **kwargs):
            calls.append(req)
            return json.dumps(envelope(stage='another_stage', title='synthetic_stage_09 overview')).encode()
        result = query_copilots('synthetic_stage_09', transport=network)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(len(result['unverified_candidates']), 1)
        self.assertFalse(result['stage_match_verified'])

    def test_activity_text_uses_document_not_level_keyword(self):
        tools = ResearchTools(self.config, self.directory, level_query=Mock(side_effect=AssertionError('Text mode')))
        calls = []
        def network(req, **kwargs):
            calls.append(req)
            return json.dumps(envelope(title='测试活动，开始！ - AB-9')).encode()
        with patch('shu.copilot_catalog._http_transport', network):
            result = tools.catalog('测试活动，开始！')
        wire = parse_qs(urlsplit(calls[0].full_url).query)
        self.assertEqual(wire['document'], ['测试活动，开始！'])
        self.assertNotIn('levelKeyword', wire)
        self.assertTrue(result['ok'])
        self.assertFalse(result['catalog']['stage_match_verified'])
        self.assertEqual(result['catalog']['filter_mode'], 'document')

    def test_page_parameter_and_source_are_bounded(self):
        calls = []
        def network(req, **kwargs):
            calls.append(req)
            return json.dumps(envelope()).encode()
        result = query_copilots('synthetic_stage_09', page=2, transport=network)
        self.assertEqual(parse_qs(urlsplit(calls[0].full_url).query)['page'], ['2'])
        self.assertEqual(result['page'], 2)
        self.assertEqual(result['total'], 7)
        self.assertTrue(result['has_more'])
        for bad in (0, -1, 11, True, '2'):
            with self.assertRaises(ValueError):
                query_copilots('synthetic_stage_09', page=bad, transport=network)

    def test_download_returns_real_document_title_and_code(self):
        tools = ResearchTools(self.config, self.directory,
                              transport=lambda *args, **kwargs: json.dumps({
                                  'status_code': 200, 'data': {'id': 42, 'content': json.dumps({
                                      'stage_name': 'synthetic_stage_09', 'doc': {'title': 'AB-9 sample'},
                                      'opers': [], 'actions': [{'type': 'SpeedUp'}]})}}).encode())
        tools._discover({'page': 'https://prts.plus/?op=42'}, operation='catalog', source='test')
        result = tools.download_copilot('https://prts.plus/?op=42')
        self.assertEqual(result['title'], 'AB-9 sample')
        self.assertEqual(result['mystery_code'], 'prts://42')
        self.assertEqual(result['page_url'], 'https://prts.plus/?op=42')
        self.assertEqual(json.loads(Path(result['path']).read_text(encoding='utf-8'))['stage_name'],
                         'synthetic_stage_09')

    def test_download_wrong_response_id_is_not_saved(self):
        tools = ResearchTools(self.config, self.directory,
                              transport=lambda *args, **kwargs: json.dumps({
                                  'status_code': 200, 'data': {'id': 43, 'content': json.dumps({
                                      'stage_name': 'wrong', 'actions': [{'type': 'SpeedUp'}]})}}).encode())
        tools._discover({'page': 'https://prts.plus/?op=42'}, operation='catalog', source='test')
        with self.assertRaises(ResearchToolError):
            tools.download_copilot('https://prts.plus/?op=42')
        self.assertFalse((self.directory / 'copilots').exists())


if __name__ == '__main__':
    unittest.main()

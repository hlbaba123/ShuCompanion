"""Candidate coverage, resumability and credential-free catalog integration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from shu.providers import ProviderConfig
from shu.research_tools import ResearchTools, ResearchToolError


def candidate(identifier, stage='unit_stage'):
    return {'id': identifier, 'title': f'candidate {identifier}', 'stage_name': stage,
            'source': f'https://prts.maa.plus/copilot/get/{identifier}',
            'page_url': f'https://prts.plus/?op={identifier}',
            'mystery_code': f'prts://{identifier}', 'available': True,
            'operators': [{'name': 'fixture operator', 'skill': 2,
                           'requirements': {'elite': 2, 'level': 60, 'skill_level': 10}}],
            'groups': [], 'hints': ['catalog metadata'], 'requires_download': True}


def page(ids, *, total, has_more, stage='unit_stage', unverified=()):
    return {'source': 'https://prts.maa.plus/copilot/query?levelKeyword=' + stage,
            'candidates': [candidate(i, stage) for i in ids],
            'unverified_candidates': [candidate(i, 'other_stage') for i in unverified],
            'total': total, 'has_more': has_more, 'raw_count': len(ids) + len(unverified),
            'filter_status': 'unsupported_filter' if unverified else 'exact_stage_id',
            'stage_match_verified': not unverified}


class CopilotPoolTests(unittest.TestCase):
    def test_catalog_read_after_complete_pool_is_local_not_a_second_public_request(self):
        with patch('shu.research_tools.query_copilots',return_value=page(range(1,20),total=19,has_more=False)) as query:
            tools=self.tools();tools.candidates('AB-9');result=tools.catalog('AB-9')
            self.assertEqual(query.call_count,1)
            self.assertTrue(result['cached'])
            self.assertEqual(len(result['catalog']['candidates']),5)
            self.assertEqual(result['catalog']['total'],19)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.config = ProviderConfig()
        self.config.resolve_key = Mock(side_effect=AssertionError('No key for public pool'))
        self.resolution = {'levels': [{'code': 'AB-9', 'stage_id': 'unit_stage',
                                      'level_id': 'activities/fixture/level'}]}

    def tools(self):
        return ResearchTools(self.config, self.directory,
                             level_query=lambda query, directory: self.resolution)

    def test_collects_whole_public_pool_once_and_keeps_full_summary_on_disk(self):
        with patch('shu.research_tools.query_copilots', return_value=page(range(1, 20), total=19, has_more=False)) as query:
            tools = self.tools()
            result = tools.candidates('AB-9')
        self.assertTrue(result['ok'])
        self.assertEqual(result['coverage']['collected'], 19)
        self.assertEqual(result['coverage']['total'], 19)
        self.assertTrue(result['coverage']['complete'])
        self.assertIsNone(result['coverage']['next_page'])
        self.assertLessEqual(len(result['preview']), 10)
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 7500)
        raw = Path(result['pool_ref']['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['pool_ref']['sha256'])
        pool = json.loads(raw)
        self.assertEqual(len(pool['candidates']), 19)
        self.assertEqual(pool['candidates'][-1]['operators'][0]['requirements']['skill_level'], 10)
        self.assertNotIn('actions', raw.decode())
        self.assertEqual(query.call_args.kwargs['limit'], 50)
        self.config.resolve_key.assert_not_called()

    def test_continues_past_hundred_and_deduplicates_instead_of_claiming_complete(self):
        responses = [page(range(1, 51), total=110, has_more=True),
                     page(range(50, 100), total=110, has_more=True),
                     page(range(100, 111), total=110, has_more=False)]
        with patch('shu.research_tools.query_copilots', side_effect=responses) as query:
            tools = self.tools()
            first = tools.candidates('AB-9')
            self.assertEqual(first['coverage']['collected'], 99)
            self.assertFalse(first['coverage']['complete'])
            self.assertEqual(first['coverage']['next_page'], 3)
            self.assertEqual(query.call_count, 2)
            result = tools.candidates('AB-9')
        self.assertEqual(result['coverage']['collected'], 110)
        self.assertTrue(result['coverage']['complete'])

    def test_completed_pool_is_reusable_across_alias_and_new_research_instance(self):
        with patch('shu.research_tools.query_copilots', return_value=page([1, 2], total=2, has_more=False)) as query:
            tools = self.tools()
            first = tools.candidates('AB-9')
            alias = tools.candidates('unit_stage')
            reloaded = self.tools().candidates('AB-9')
        self.assertEqual(first['pool_ref'], alias['pool_ref'])
        self.assertEqual(first['pool_ref'], reloaded['pool_ref'])
        self.assertTrue(alias['cached'])
        self.assertTrue(reloaded['cached'])
        self.assertEqual(query.call_count, 1)

    def test_shared_task_pool_continues_across_dispatches_without_restarting_pages(self):
        shared = self.directory / 'task' / 'candidate-cache'
        def dispatch(name):
            tools = ResearchTools(self.config, self.directory / name,
                                  level_query=lambda query, directory: self.resolution)
            tools.set_pool_directory(shared)
            return tools
        responses = [page(range(1, 51), total=125, has_more=True),
                     page(range(51, 101), total=125, has_more=True),
                     page(range(101, 126), total=125, has_more=False)]
        with patch('shu.research_tools.query_copilots', side_effect=responses) as query:
            first = dispatch('dispatch-1')
            initial = first.candidates('AB-9')
            second = dispatch('dispatch-2')
            second.register_pool_ref(initial['pool_ref'])
            result = second.candidates('AB-9')
        self.assertEqual([call.kwargs['page'] for call in query.call_args_list], [1, 2, 3])
        self.assertEqual(result['coverage']['collected'], 125)
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(second.cache_dir, self.directory / 'dispatch-2')
        self.assertTrue(Path(result['pool_ref']['path']).is_relative_to(shared))

    def test_prior_pool_registers_for_local_read_and_download_discovery_only_in_fixed_root(self):
        shared = self.directory / 'task' / 'candidate-cache'
        first = self.tools()
        first.set_pool_directory(shared)
        with patch('shu.research_tools.query_copilots', return_value=page(range(1, 26), total=25, has_more=False)):
            saved = first.candidates('AB-9')
        second = ResearchTools(self.config, self.directory / 'next-dispatch')
        second.set_pool_directory(shared)
        with self.assertRaises(ResearchToolError):
            second.read_pool_page(saved['pool_ref']['pool_id'])
        second.register_pool_ref(saved['pool_ref'])
        result = second.read_pool_page(saved['pool_ref']['pool_id'], offset=20, limit=5)
        self.assertEqual([item['id'] for item in result['candidates']], [21, 22, 23, 24, 25])
        self.assertIn('https://prts.maa.plus/copilot/get/25', second.discovered_copilot_urls)
        fake = {**saved['pool_ref'], 'path': str(self.directory / 'unrelated.json')}
        with self.assertRaises(ResearchToolError):
            second.register_pool_ref(fake)
        foreign_task = ResearchTools(self.config, self.directory / 'other-task')
        foreign_task.set_pool_directory(self.directory / 'other-task' / 'candidate-cache')
        with self.assertRaises(ResearchToolError):
            foreign_task.register_pool_ref(saved['pool_ref'])
        self.config.resolve_key.assert_not_called()

    def test_explicitly_registered_incomplete_pool_resumes_after_freshness_ttl(self):
        shared = self.directory / 'task' / 'candidate-cache'
        with patch('shu.research_tools.query_copilots', side_effect=[
                page(range(1, 51), total=125, has_more=True),
                page(range(51, 101), total=125, has_more=True),
                page(range(101, 126), total=125, has_more=False)]) as query:
            first = self.tools()
            first.set_pool_directory(shared)
            with patch('time.time', return_value=1000):
                saved = first.candidates('AB-9')
            second = ResearchTools(self.config, self.directory / 'next-dispatch',
                                   level_query=lambda query, directory: self.resolution)
            second.set_pool_directory(shared)
            with patch('time.time', return_value=1600):
                second.register_pool_ref(saved['pool_ref'])
                result = second.candidates('AB-9')
        self.assertEqual([call.kwargs['page'] for call in query.call_args_list], [1, 2, 3])
        self.assertTrue(result['coverage']['complete'])

    def test_registered_pool_identity_must_match_its_hashed_query(self):
        shared = self.directory / 'task' / 'candidate-cache'
        tools = self.tools()
        tools.set_pool_directory(shared)
        with patch('shu.research_tools.query_copilots', return_value=page([1], total=1, has_more=False)):
            saved = tools.candidates('AB-9')
        data = json.loads(Path(saved['pool_ref']['path']).read_bytes())
        data['stage_id'] = 'different_stage'
        raw = json.dumps(data).encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = Path(saved['pool_ref']['path']).with_name(digest + '.json')
        path.write_bytes(raw)
        with self.assertRaises(ResearchToolError):
            tools.register_pool_ref({**saved['pool_ref'], 'path': str(path), 'sha256': digest})

    def test_pool_page_registers_every_selected_source_for_existing_downloader(self):
        with patch('shu.research_tools.query_copilots', return_value=page(range(1, 26), total=25, has_more=False)):
            tools = self.tools()
            result = tools.candidates('AB-9')
        detail = tools.read_pool_page(result['pool_ref']['pool_id'], offset=20, limit=5)
        self.assertEqual([item['id'] for item in detail['candidates']], [21, 22, 23, 24, 25])
        source = 'https://prts.maa.plus/copilot/get/25'
        self.assertIn(source, tools.discovered_copilot_urls)
        self.assertEqual(tools.discovery_sources[source][0]['operation'], 'candidates')

    def test_unknown_pool_or_out_of_range_page_never_reads_arbitrary_path(self):
        tools = self.tools()
        for value in ('../../secrets', str(self.directory / 'other.json'), 'a' * 64):
            with self.subTest(value=value), self.assertRaises(ResearchToolError):
                tools.read_pool_page(value)

    def test_cancel_before_request_does_not_resolve_stage_or_query(self):
        tools = self.tools()
        stop = threading.Event()
        stop.set()
        with patch('shu.research_tools.query_copilots') as query:
            result = tools.candidates('AB-9', cancel_event=stop)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['ok'])
        self.assertEqual(tools.stats['levels'], 0)
        query.assert_not_called()

    def test_cancel_between_pages_saves_resumable_first_page(self):
        stop = threading.Event()
        def first_page(*args, **kwargs):
            stop.set()
            return page(range(1, 51), total=75, has_more=True)
        with patch('shu.research_tools.query_copilots', side_effect=first_page):
            tools = self.tools()
            result = tools.candidates('AB-9', cancel_event=stop)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['coverage']['collected'], 50)
        self.assertEqual(result['coverage']['next_page'], 2)

    def test_total_change_is_explicit_and_never_claims_snapshot_complete(self):
        responses = [page(range(1, 51), total=60, has_more=True),
                     page(range(51, 62), total=61, has_more=False)]
        with patch('shu.research_tools.query_copilots', side_effect=responses):
            result = self.tools().candidates('AB-9')
        self.assertTrue(result['coverage']['total_changed'])
        self.assertTrue(result['coverage']['exhausted'])
        self.assertFalse(result['coverage']['complete'])
        self.assertIsNone(result['coverage']['next_page'])

    def test_unverified_stage_and_server_shortfall_are_never_complete(self):
        with patch('shu.research_tools.query_copilots', return_value=page([1], total=3, has_more=False, unverified=[2])):
            result = self.tools().candidates('AB-9')
        self.assertEqual(result['coverage']['collected'], 1)
        self.assertEqual(result['coverage']['unverified'], 1)
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(result['coverage']['exhausted'])

    def test_network_failure_preserves_progress_and_next_page(self):
        from shu.copilot_catalog import CopilotCatalogError
        responses = [page(range(1, 51), total=75, has_more=True),
                     CopilotCatalogError('private upstream body', reason='timeout')]
        with patch('shu.research_tools.query_copilots', side_effect=responses):
            result = self.tools().candidates('AB-9')
        self.assertFalse(result['ok'])
        self.assertEqual(result['coverage']['collected'], 50)
        self.assertEqual(result['coverage']['next_page'], 2)
        self.assertNotIn('private upstream', json.dumps(result))

    def test_second_page_preserves_its_own_discovery_source(self):
        first = page(range(1, 51), total=51, has_more=True)
        second = page([51], total=51, has_more=False)
        first['source'] += '&page=1'
        second['source'] += '&page=2'
        with patch('shu.research_tools.query_copilots', side_effect=[first, second]):
            tools = self.tools()
            tools.candidates('AB-9')
        self.assertEqual(tools.discovery_sources['https://prts.maa.plus/copilot/get/51'][0]['source'],
                         'https://prts.maa.plus/copilot/query?levelKeyword=unit_stage&page=2')

    def test_corrupted_pool_is_rejected_for_reading(self):
        with patch('shu.research_tools.query_copilots', return_value=page([1], total=1, has_more=False)):
            tools = self.tools()
            result = tools.candidates('AB-9')
        Path(result['pool_ref']['path']).write_text('{}', encoding='utf-8')
        with self.assertRaises(ResearchToolError):
            tools.read_pool_page(result['pool_ref']['pool_id'])

    def test_levels_reuses_display_and_internal_alias_without_spending_lookup_budget(self):
        tools = self.tools()
        first = tools.levels('AB-9')
        for alias in ('unit_stage', 'activities/fixture/level', 'ab-9'):
            self.assertEqual(tools.levels(alias)['data']['levels'], first['data']['levels'])
        self.assertEqual(tools.stats['levels'], 1)

    def test_resolved_level_path_alias_uses_stage_filter_instead_of_text_filter(self):
        tools = self.tools()
        tools.levels('AB-9')
        with patch('shu.research_tools.query_copilots', return_value=page([1], total=1, has_more=False)) as query:
            tools.candidates('activities/fixture/level')
        self.assertEqual(query.call_args.args, ('unit_stage',))
        self.assertIsNone(query.call_args.kwargs['document'])
        self.assertEqual(tools.stats['levels'], 1)

    def test_builtin_levels_receives_explicit_owned_cache_root(self):
        tools = ResearchTools(self.config, self.directory)
        with patch('shu.level_catalog.find_levels', return_value=self.resolution) as lookup:
            tools.levels('AB-9')
        self.assertEqual(lookup.call_args.kwargs['cache_root'], self.directory)

    def test_fetch_discovered_copilot_downloads_directly_without_paid_api_or_key(self):
        source = 'https://prts.maa.plus/copilot/get/22'
        body = {'status_code': 200, 'data': {'id': 22, 'content': json.dumps({
            'stage_name': 'unit_stage', 'opers': [], 'actions': [{'type': 'Deploy'}]})}}
        calls = []
        def network(req, **kwargs):
            calls.append(req)
            return json.dumps(body).encode()
        tools = ResearchTools(self.config, self.directory, transport=network)
        tools._discover({'source': source, 'page_url': 'https://prts.plus/?op=22'},
                        operation='catalog', source='https://prts.maa.plus/copilot/query')
        result = tools.fetch('https://prts.plus/?op=22')
        self.assertEqual(result['tool'], 'download_copilot')
        self.assertEqual(result['source_url'], source)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].get_header('Authorization'))
        self.assertEqual(tools.stats['fetch'], 0)
        self.config.resolve_key.assert_not_called()

    def test_fetch_unknown_copilot_does_not_resolve_key_or_network(self):
        network = Mock(side_effect=AssertionError('Unknown copilot must not fetch'))
        tools = ResearchTools(self.config, self.directory, transport=network)
        for url in ('https://prts.plus/?op=22', 'https://prts.maa.plus/copilot/get/22'):
            with self.assertRaises(ResearchToolError):
                tools.fetch(url)
        self.assertEqual(tools.stats['fetch'], 0)
        self.config.resolve_key.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Bounded research retries use fresh public cursors and preserve partial proof."""
import json
from pathlib import Path
from unittest.mock import patch

from tests.test_code_pair_search import PairIntegration
from shu.code_delivery import CodeTools
from shu.copilot_catalog import parse_candidate
from shu.copilot_pool import CopilotPool
from shu.providers import ProviderConfig
from shu.research_tools import ResearchTools, ResearchToolError


class RetrySearchTests(PairIntegration):
    def research(self):
        tools = ResearchTools(ProviderConfig(), self.root / 'research')
        tools.set_pool_directory(self.root / 'search-cache')
        return tools

    def test_second_round_refreshes_once_then_continues_the_new_cursor(self):
        research = self.research()
        calls = []
        def page(query, **kwargs):
            calls.append(kwargs['page'])
            if len(calls) == 1:
                return {'candidates': [], 'source': 'https://prts.maa.plus/copilot/query',
                        'total': 0, 'raw_count': 0, 'has_more': False}
            number = kwargs['page']
            candidate = parse_candidate({'id': number, 'available': True, 'content': json.dumps({
                'stage_name': 'unit_stage', 'difficulty': 1, 'opers': [{'name': '甲'}]})})
            return {'candidates': [candidate], 'source': 'https://prts.maa.plus/copilot/query',
                    'total': 2, 'raw_count': 1, 'has_more': number == 1}
        tools = CodeTools(self.store, self.plans, research, self.rosters)
        with patch('shu.research_tools.query_copilots', side_effect=page):
            first = tools.find_pair(self.lease, clock=self.clock)
            self.assertEqual(first['status'], 'no_jobs_yet')
            old_ref = research._pool_refs[CopilotPool.identity('unit_stage', None)]
            old_bytes = Path(old_ref['path']).read_bytes()
            self.plans.close_step(self.lease, first)
            reopened = self.plans.retry_failed(self.task, expected_revision=1, expected_epoch=1,
                                              dispatch_id=self.lease['dispatch_id'])
            self.assertEqual(reopened, [self.lease['step_id']])
            lease = self.plans.claim_next(self.task, expected_revision=1, expected_epoch=1,
                                         dispatch_id=self.lease['dispatch_id'])
            second = tools.find_pair(lease, clock=self.clock)
        self.assertEqual(second['status'], 'two_verified')
        self.assertEqual(second['attempt'], 2)
        self.assertEqual(calls, [1, 1, 2], 'Retry must refresh page 1 once, then continue to page 2')
        self.assertEqual(Path(old_ref['path']).read_bytes(), old_bytes)
        self.assertEqual(CopilotPool(research._pool_directory).read(old_ref)['candidates'], [])

    def test_network_failure_after_a_primary_preserves_the_verified_partial(self):
        pool = self.ref([self.candidate(1)])
        class Research:
            def candidates_for_stage(_, stage, cache_only=False, before_fetch=None, **kwargs):
                if cache_only:
                    return {'pool_ref': pool, 'coverage': {'complete': False}, 'pages_fetched': 0}
                before_fetch(2)
                raise ResearchToolError('公开目录暂时不可用')
        result = CodeTools(self.store, self.plans, Research(), self.rosters).find_pair(self.lease, clock=self.clock)
        self.assertEqual(result['status'], 'primary_only')
        self.assertEqual(self.store.read_code(result['primary_ref'])['id'], 1)
        self.assertIsNone(result['backup_ref'])
        self.assertEqual(result['error_classification'], 'catalog_unavailable')
        self.assertEqual(result['backup_reason'], 'network_error')
        self.assertEqual(result['pages_attempted'], 1)
        self.assertEqual(result['pages_fetched'], 0)

    def test_transport_error_is_a_search_gap_with_error_evidence(self):
        with patch('shu.research_tools.query_copilots', side_effect=TimeoutError('timeout')):
            result = CodeTools(self.store, self.plans, self.research(), self.rosters).find_pair(
                self.lease, clock=self.clock)
        self.assertEqual(result['status'], 'search_incomplete')
        self.assertEqual(result.get('error_classification'), 'catalog_unavailable')
        self.assertEqual(result.get('reason'), 'network_error')
        self.assertEqual(result.get('attempt'), 1)
        self.assertEqual(result.get('pages_attempted'), 1)
        self.assertEqual(result.get('pages_fetched'), 0)

    def test_explicit_refresh_does_not_discard_the_previous_pool_file(self):
        research = self.research()
        empty = {'candidates': [], 'source': 'https://prts.maa.plus/copilot/query',
                 'total': 0, 'raw_count': 0, 'has_more': False}
        new = {**empty, 'candidates': [self.candidate(2)], 'total': 1, 'raw_count': 1}
        with patch('shu.research_tools.query_copilots', side_effect=[empty, new]):
            first = research.candidates_for_stage(self.stage, before_fetch=lambda page: True)
            second = research.candidates_for_stage(self.stage, refresh=True, before_fetch=lambda page: True)
        self.assertEqual(first['coverage']['total'], 0)
        self.assertEqual(second['coverage']['total'], 1)
        self.assertEqual(CopilotPool(research._pool_directory).read(first['pool_ref'])['candidates'], [])


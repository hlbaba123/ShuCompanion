"""Pool evidence survives Windows MAX_PATH and reports local IO failures."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from shu.code_delivery import CodeTools
from shu.code_presentation import render_code_summary
from shu.copilot_pool import CopilotPool, CopilotPoolError
from shu.providers import ProviderConfig
from shu.research_tools import ResearchToolError, ResearchTools
from tests.test_code_pair_search import PairIntegration
from tests.test_copilot_pool import page


class PoolLongPathTests(unittest.TestCase):
    def setUp(self):
        # The short parent lets legacy Windows hosts create and clean the test
        # root; all pool IO itself must support the real deep normal path.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.deep = self.root / ('中文 空格资料' * 7) / ('task-evidence-' * 6) / 'candidate-cache'
        while len(str(self.deep / 'candidate-pools')) <= 270:
            self.deep /= 'nested-task'
        self.addCleanup(self._clean_deep)

    def _clean_deep(self):
        if os.name == 'nt':
            import shutil
            shutil.rmtree('\\\\?\\' + str(self.root), ignore_errors=True)

    def test_real_deep_pool_roundtrip_cache_and_immutable_prior_ref(self):
        pool = CopilotPool(self.deep)
        try:
            first, _ = pool.collect('unit_stage', None,
                query=lambda *args, **kwargs: page([1], total=1, has_more=False))
        except CopilotPoolError as exc:
            self.fail(f'Actual Windows deep-path pool must save: {exc}')
        self.assertGreater(len(first['pool_ref']['path']), 300)
        self.assertFalse(first['pool_ref']['path'].startswith('\\\\?\\'))
        self.assertEqual(pool.read(first['pool_ref'])['candidates'][0]['id'], 1)
        network = Mock(side_effect=AssertionError('A complete pool must remain local'))
        cached, _ = CopilotPool(self.deep).collect('unit_stage', None, query=network)
        self.assertEqual(cached['pool_ref'], first['pool_ref'])
        self.assertTrue(cached['cached'])
        refreshed, _ = pool.collect('unit_stage', None, refresh=True,
            query=lambda *args, **kwargs: page([2], total=1, has_more=False))
        self.assertNotEqual(refreshed['pool_ref'], first['pool_ref'])
        self.assertEqual(pool.read(first['pool_ref'])['candidates'][0]['id'], 1)
        self.assertEqual(CopilotPool(self.deep).read(refreshed['pool_ref'])['candidates'][0]['id'], 2)

    def test_deep_pool_keeps_root_hash_and_identity_checks(self):
        pool = CopilotPool(self.deep)
        try:
            result, _ = pool.collect('unit_stage', None,
                query=lambda *args, **kwargs: page([1], total=1, has_more=False))
        except CopilotPoolError as exc:
            self.fail(f'Actual Windows deep-path pool must save before validation: {exc}')
        ref = result['pool_ref']
        with self.assertRaises(CopilotPoolError):
            pool.read({**ref, 'path': str(self.root / Path(ref['path']).name)})
        with self.assertRaises(CopilotPoolError):
            pool.read({**ref, 'pool_id': 'a' * 64})
        target = Path('\\\\?\\' + ref['path']) if os.name == 'nt' else Path(ref['path'])
        target.write_bytes(b'{}')
        with self.assertRaises(CopilotPoolError):
            pool.read(ref)


class PoolLocalErrorTests(PairIntegration):
    def research(self):
        research = ResearchTools(ProviderConfig(), self.root / 'research')
        research.set_pool_directory(self.root / 'search-cache')
        return research

    def test_write_failure_is_local_io_with_safe_numeric_evidence(self):
        research = self.research()
        failure = OSError(errno.ENOSPC, 'disk full: private-path Bearer sk-do-not-disclose')
        with patch.object(Path, 'write_bytes', side_effect=failure):
            result = CodeTools(
                self.store, self.plans, research, self.rosters).find_pair(self.lease, clock=self.clock)
        self.assertEqual(result['status'], 'search_incomplete')
        self.assertEqual(result['error_classification'], 'local_io_error')
        self.assertEqual(result['reason'], 'local_io_error')
        self.assertEqual(result['pages_attempted'], 0)
        self.assertEqual(result['errors'][-1]['io_operation'], 'write')
        self.assertEqual(result['errors'][-1]['errno'], errno.ENOSPC)
        self.assertNotIn('private-path', str(result))
        self.assertNotIn('sk-do-not-disclose', str(result))

    def test_read_failure_retains_local_error_through_research_handoff(self):
        ref = self.ref([self.candidate(1)])
        research = ResearchTools(ProviderConfig(), self.root / 'research')
        research.set_pool_directory(self.root / 'cache')
        with patch.object(Path, 'open', side_effect=PermissionError(errno.EACCES, 'secret path')):
            with self.assertRaises(ResearchToolError) as caught:
                research.register_pool_ref(ref)
        self.assertEqual(getattr(caught.exception, 'error_classification', None), 'local_io_error')
        self.assertEqual(getattr(caught.exception, 'io_operation', None), 'read')
        self.assertEqual(getattr(caught.exception, 'errno', None), errno.EACCES)
        self.assertNotIn('secret path', str(caught.exception))

    def test_cached_permission_failure_does_not_trigger_a_public_request(self):
        pool = CopilotPool(self.root / 'cache')
        pool.collect('unit_stage', None,
            query=lambda *args, **kwargs: page([1], total=1, has_more=False))
        requests = []
        def public(*args, **kwargs):
            requests.append(kwargs['page'])
            return page([2], total=1, has_more=False)
        with patch.object(Path, 'open', side_effect=PermissionError(errno.EACCES, 'secret path')):
            with self.assertRaises(CopilotPoolError) as caught:
                pool.collect('unit_stage', None, query=public)
        self.assertEqual(caught.exception.error_classification, 'local_io_error')
        self.assertEqual(caught.exception.errno, errno.EACCES)
        self.assertEqual(requests, [])

    def test_cleanup_failure_does_not_replace_original_write_error(self):
        pool = CopilotPool(self.root / 'cache')
        with patch.object(Path, 'write_bytes', side_effect=OSError(errno.ENOSPC, 'secret disk')):
            with patch.object(Path, 'unlink', side_effect=PermissionError(errno.EACCES, 'secret cleanup')):
                with self.assertRaises(CopilotPoolError) as caught:
                    pool.collect('unit_stage', None,
                        query=lambda *args, **kwargs: page([1], total=1, has_more=False))
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertNotIn('secret', str(caught.exception))

    def test_save_path_resolution_failure_has_safe_local_metadata(self):
        pool = CopilotPool(self.root / 'cache')
        with patch.object(Path, 'resolve', side_effect=PermissionError(errno.EACCES, 'private root')):
            with self.assertRaises(CopilotPoolError) as caught:
                pool._save({'pool_id': 'a' * 64})
        self.assertEqual(caught.exception.error_classification, 'local_io_error')
        self.assertEqual(caught.exception.errno, errno.EACCES)
        self.assertNotIn('private root', str(caught.exception))

    def test_initial_pool_root_failure_is_reported_without_site_request(self):
        research = self.research()
        with patch.object(Path, 'resolve', side_effect=PermissionError(errno.EACCES, 'private root')):
            result = CodeTools(self.store, self.plans, research, self.rosters).find_pair(
                self.lease, clock=self.clock)
        self.assertEqual(result['reason'], 'local_io_error')
        self.assertEqual(result['errors'][-1]['io_operation'], 'resolve')
        self.assertEqual(result['pages_attempted'], 0)
        self.assertNotIn('private root', str(result))

    def test_plain_local_oserror_is_not_reported_as_site_failure(self):
        class Research:
            def candidates_for_stage(self, *args, **kwargs):
                raise PermissionError(errno.EACCES, 'private path')
        result = CodeTools(self.store, self.plans, Research(), self.rosters).find_pair(
            self.lease, clock=self.clock)
        self.assertEqual(result['reason'], 'local_io_error')
        self.assertEqual(result['errors'][-1]['errno'], errno.EACCES)
        self.assertEqual(result['pages_attempted'], 0)
        self.assertNotIn('private', str(result))

    def test_local_io_failure_is_visible_as_local_in_user_summary(self):
        rendered = render_code_summary({'summary': '0/1', 'items': [{
            'stage_code': 'A-1', 'status': 'search_incomplete', 'reason': 'local_io_error'}]})
        self.assertIn('本地', rendered)
        self.assertNotIn('作业站连接', rendered)

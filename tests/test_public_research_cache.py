"""Short public caches do not relocate task-bound copilot evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from shu.level_catalog import find_levels
from shu.providers import ProviderConfig
from shu.research_tools import ResearchToolError, ResearchTools


DATA_URL = 'https://prts.maa.plus/copilot/get/73921'
FETCH_URL = 'https://prts.wiki/w/OfflineStage'
LEVELS = {'status_code': 200, 'data': [
    {'stage_id': 'synthetic_stage', 'level_id': 'synthetic_level',
     'cat_three': 'AB-9', 'cat_two': '离线活动', 'cat_one': '离线分类', 'name': '离线关卡'}]}


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


class Network:
    def __init__(self, *values):
        self.values, self.calls = list(values), []

    def __call__(self, request, **options):
        self.calls.append(request.full_url)
        if not self.values:
            raise AssertionError('Unexpected real IO')
        return encoded(self.values.pop(0))


class PublicCacheTests(unittest.TestCase):
    def setUp(self):
        staging = Path(__file__).resolve().parents[2] / '.pet-temp'
        staging.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=staging)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        task = self.root / 'task'
        self.task = task / ('d' * (181 - len(str(task)) - 1))
        self.public = self.root / 'public'
        self.config = ProviderConfig(api_key='sk-offline-cache-test')
        self.config.resolve_key = Mock(return_value='sk-offline-cache-test')

    def tool(self, *values, task=None, public=None):
        network = Network(*values)
        tool = ResearchTools(self.config, task or self.task, transport=network,
                             public_cache_root=public or self.public)
        return tool, network

    def test_deep_task_search_fetch_use_short_full_hash_cache_and_download_stays_in_task(self):
        copilot = {'stage_name': 'synthetic_stage', 'actions': [{'type': 'SpeedUp'}],
                   'doc': {'title': '离线作业'}, 'opers': [{'name': '离线干员'}]}
        tool, network = self.tool({'results': [{'url': DATA_URL}]}, {'content': 'offline page'}, copilot)
        self.assertEqual(len(str(self.task)), 181)
        tool.search('离线查询')
        tool.fetch(FETCH_URL)
        downloaded = tool.download_copilot(DATA_URL)
        for key in ('search:离线查询', 'fetch:' + FETCH_URL):
            path = tool._cache_path(key)
            self.assertTrue(path.is_relative_to(self.public))
            self.assertEqual(path.stem, hashlib.sha256(key.encode()).hexdigest())
            self.assertTrue(path.is_file())
        path = Path(downloaded['path'])
        self.assertTrue(path.is_relative_to(self.task))
        self.assertFalse(path.is_relative_to(self.public))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), downloaded['sha256'])
        self.assertFalse((self.task / 'research').exists())
        self.assertEqual(len(network.calls), 3)

    def test_shared_public_hits_need_no_key_or_request_across_dispatches(self):
        first, _ = self.tool({'results': []}, {'content': 'cached'})
        first.search('cached query')
        first.fetch(FETCH_URL)
        self.config.resolve_key = Mock(side_effect=AssertionError('cache hit must not read Key'))
        second, network = self.tool(task=self.root / 'another-task')
        self.assertTrue(second.search('cached query')['cached'])
        self.assertTrue(second.fetch(FETCH_URL)['cached'])
        self.assertEqual(network.calls, [])

    def test_legacy_search_fetch_fallback_is_read_only_and_retains_ttl(self):
        legacy = self.root / 'legacy'
        network = Network({'results': []}, {'content': 'legacy'})
        before = time.time()
        old = ResearchTools(self.config, legacy, transport=network)
        with patch('shu.research_tools.time.time', return_value=before):
            old.search('legacy query')
            old.fetch(FETCH_URL)
        saved = {path: path.read_bytes() for path in legacy.rglob('*.json')}
        new, blocked = self.tool(task=legacy)
        with patch('shu.research_tools.time.time', return_value=before + 299):
            self.assertTrue(new.search('legacy query')['cached'])
            self.assertTrue(new.fetch(FETCH_URL)['cached'])
        self.assertFalse(self.public.exists())
        self.assertEqual(blocked.calls, [])
        refreshed, _ = self.tool({'results': [{'title': 'fresh'}]}, task=legacy)
        with patch('shu.research_tools.time.time', return_value=before + 300):
            self.assertFalse(refreshed.search('legacy query')['cached'])
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)
        self.assertTrue((self.public / 'research').is_dir())

    def test_levels_cache_uses_short_authorized_root_and_default_transport_stays_public(self):
        tool, network = self.tool()
        with patch('shu.level_catalog._http_transport', return_value=encoded(LEVELS)) as transport:
            result = tool.levels('AB-9')
        self.assertTrue(result['ok'])
        transport.assert_called_once()
        request = transport.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))
        self.assertTrue((self.public / 'levels' / 'level-catalog-cache.json').is_file())
        self.assertFalse((self.task / 'level-catalog-cache.json').exists())
        second, _ = self.tool(task=self.root / 'second')
        with patch('shu.level_catalog._http_transport', side_effect=AssertionError('disk cache expected')):
            self.assertTrue(second.levels('AB-9')['ok'])
        self.assertEqual(network.calls, [])
        self.config.resolve_key.assert_not_called()

    def test_level_legacy_fallback_validates_hash_and_never_writes_old_directory(self):
        legacy = self.root / 'legacy'
        find_levels('AB-9', legacy, cache_root=legacy, transport=Network(LEVELS))
        saved = {path: path.read_bytes() for path in legacy.iterdir()}
        tool, _ = self.tool(task=legacy)
        with patch('shu.level_catalog._http_transport', side_effect=AssertionError('legacy hit expected')):
            self.assertTrue(tool.levels('AB-9')['ok'])
        self.assertFalse(self.public.exists())
        raw = next(path for path in legacy.iterdir() if path.name != 'level-catalog-cache.json')
        raw.write_bytes(b'{}')
        tampered = raw.read_bytes()
        other, _ = self.tool(task=legacy)
        with patch('shu.level_catalog._http_transport', return_value=encoded(LEVELS)) as transport:
            self.assertTrue(other.levels('AB-9')['ok'])
        transport.assert_called_once()
        self.assertEqual(raw.read_bytes(), tampered)
        manifest = legacy / 'level-catalog-cache.json'
        self.assertEqual(manifest.read_bytes(), saved[manifest])
        self.assertTrue((self.public / 'levels' / 'level-catalog-cache.json').is_file())

    def test_default_constructor_keeps_original_cache_layout(self):
        tool = ResearchTools(self.config, self.root / 'legacy', transport=Network({'results': []}))
        tool.search('legacy default')
        self.assertTrue(tool._cache_path('search:legacy default').is_relative_to(tool.cache_dir))

    def test_expired_legacy_levels_refresh_only_the_new_cache(self):
        legacy, now = self.root / 'legacy', time.time()
        with patch('shu.level_catalog.time.time', return_value=now):
            find_levels('AB-9', legacy, cache_root=legacy, transport=Network(LEVELS))
        saved = {path: path.read_bytes() for path in legacy.iterdir()}
        tool, _ = self.tool(task=legacy)
        with patch('shu.level_catalog.time.time', return_value=now + 86400), \
                patch('shu.level_catalog._http_transport', return_value=encoded(LEVELS)) as transport:
            self.assertTrue(tool.levels('AB-9')['ok'])
        transport.assert_called_once()
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)
        self.assertTrue((self.public / 'levels' / 'level-catalog-cache.json').is_file())

    def test_public_response_and_level_cache_paths_cannot_escape_the_selected_root(self):
        tool, network = self.tool()
        response_path = tool._cache_path('search:boundary')
        level_path = self.public / 'levels'
        outside = self.root / 'outside'
        resolve = Path.resolve
        def redirected(path, *args, **kwargs):
            if path in (response_path, level_path):
                return outside
            return resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', redirected), \
                patch('shu.level_catalog._http_transport') as transport:
            with self.assertRaises(ResearchToolError):
                tool.search('boundary')
            with self.assertRaises(ResearchToolError):
                tool.levels('AB-9')
        self.assertEqual(network.calls, [])
        transport.assert_not_called()
        self.config.resolve_key.assert_not_called()
        self.assertFalse(outside.exists())


if __name__ == '__main__':
    unittest.main()

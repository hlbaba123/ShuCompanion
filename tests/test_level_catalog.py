"""Offline public metadata contracts; all levels below are invented fixtures."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from shu.level_catalog import LevelCatalogError, find_levels, _NoRedirect


def payload(rows=None):
    return json.dumps({'status_code': 200, 'data': rows if rows is not None else [
        {'stage_id': 'test_internal_a', 'level_id': 'test_level_a', 'cat_one': '测试分类',
         'cat_two': '虚构活动', 'cat_three': 'TEST-7', 'name': '离线关卡'},
        {'stage_id': 'test_internal_b', 'level_id': 'test_level_b', 'cat_one': '测试分类',
         'cat_two': '另一虚构活动', 'cat_three': 'TEST-7', 'name': '另一离线关卡'},
    ]}, ensure_ascii=False).encode('utf-8')


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, req, **options):
        self.calls.append((req, options))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class LevelCatalogTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.pet-temp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)

    def test_snake_case_metadata_preserves_ambiguous_codes_and_fixed_get(self):
        raw = payload()
        net = Transport(raw)
        result = find_levels('test-7', self.cache, transport=net)
        self.assertEqual([x['stage_id'] for x in result['levels']],
                         ['test_internal_a', 'test_internal_b'])
        self.assertEqual(result['levels'][0]['level_id'], 'test_level_a')
        self.assertEqual(result['levels'][0]['event'], '虚构活动')
        self.assertEqual(result['levels'][0]['code'], 'TEST-7')
        self.assertEqual(result['raw_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertTrue(result['untrusted_content'])
        self.assertFalse(result['content_is_instruction'])
        self.assertEqual(result['total_matches'], 2)
        req, options = net.calls[0]
        self.assertEqual(req.full_url, 'https://prts.maa.plus/arknights/level')
        self.assertEqual(req.get_method(), 'GET')
        self.assertIsNone(req.data)
        self.assertIsNone(req.get_header('Authorization'))
        self.assertIsNone(req.get_header('Cookie'))
        self.assertEqual(options, {'timeout': 15, 'max_bytes': 8 * 1024 * 1024})

    def test_id_exact_and_text_contains_without_inventing_aliases(self):
        net = Transport(payload())
        self.assertEqual(len(find_levels('test_internal_a', self.cache, transport=net)['levels']), 1)
        self.assertEqual(len(find_levels('test_level_b', self.cache, transport=net)['levels']), 1)
        self.assertEqual(len(find_levels('离线', self.cache, transport=net)['levels']), 2)
        self.assertEqual(find_levels('test_internal', self.cache, transport=net)['levels'], [])
        self.assertEqual(find_levels('unknown', self.cache, transport=net)['levels'], [])
        self.assertEqual(len(net.calls), 1)

    def test_24h_cache_then_refresh(self):
        now = time.time()
        first = Transport(payload())
        with patch('shu.level_catalog.time.time', return_value=now):
            find_levels('TEST-7', self.cache, transport=first)
        blocked = Transport(RuntimeError('network must not run'))
        with patch('shu.level_catalog.time.time', return_value=now + 86399):
            self.assertEqual(find_levels('TEST-7', self.cache, transport=blocked)['cache'], 'hit')
        fresh = Transport(payload([]))
        with patch('shu.level_catalog.time.time', return_value=now + 86400):
            self.assertEqual(find_levels('TEST-7', self.cache, transport=fresh)['levels'], [])
        self.assertEqual(len(fresh.calls), 1)
        self.assertEqual(blocked.calls, [])

    def test_invalid_query_is_rejected_before_network_or_cache_creation(self):
        net = Transport(payload())
        for query in (None, 42, '', ' ', 'x' * 129, 'https://example.invalid',
                      'sk-fake-credential', 'Bearer abc', 'api_key=secret', 'a\nb', '//example.invalid'):
            with self.subTest(query=query), self.assertRaises(LevelCatalogError) as raised:
                find_levels(query, self.cache / 'new', transport=net)
            self.assertEqual(raised.exception.reason, 'query')
        self.assertEqual(net.calls, [])
        self.assertFalse((self.cache / 'new').exists())

    def test_bounded_output_and_eight_results_without_extra_remote_fields(self):
        rows = [{'stage_id': 'test_' + str(i), 'level_id': i, 'cat_one': '分类' * 999,
                 'cat_two': '活动' * 999, 'cat_three': 'TEST', 'name': '测试' * 999,
                 'secret_extra': 'must not expose'} for i in range(20)]
        result = find_levels('TEST', self.cache, transport=Transport(payload(rows)))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 6000)
        self.assertEqual(len(result['levels']), 8)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['total_matches'], 20)
        self.assertNotIn('must not expose', json.dumps(result))

    def test_json_size_and_schema_errors_do_not_replace_valid_cache(self):
        now = time.time()
        with patch('shu.level_catalog.time.time', return_value=now):
            find_levels('TEST-7', self.cache, transport=Transport(payload()))
        original = {p.name: p.read_bytes() for p in self.cache.iterdir()}
        cases = [(b'x' * (8 * 1024 * 1024 + 1), 'response'), (b'bad json', 'json'),
                 (b'{"status_code":200,"data":[],"x":NaN}', 'json'),
                 (b'{"status_code":200,"data":{}}', 'schema'),
                 (b'{"status_code":500,"data":[]}', 'schema'),
                 (b'{"status_code":200,"data":[{"stageId":"wrong"}]}', 'schema')]
        for raw, reason in cases:
            with self.subTest(reason=reason), patch('shu.level_catalog.time.time', return_value=now + 86401):
                with self.assertRaises(LevelCatalogError) as raised:
                    find_levels('TEST-7', self.cache, transport=Transport(raw))
                self.assertEqual(raised.exception.reason, reason)
            self.assertEqual({p.name: p.read_bytes() for p in self.cache.iterdir()}, original)

    def test_http_timeout_network_errors_are_sanitized(self):
        for exc, reason in ((TimeoutError('private key'), 'timeout'),
                            (HTTPError('https://secret.invalid', 302, 'secret', {}, None), 'http'),
                            (RuntimeError('private key'), 'network')):
            with self.subTest(reason=reason), self.assertRaises(LevelCatalogError) as raised:
                find_levels('TEST', self.cache, transport=Transport(exc))
            self.assertEqual(raised.exception.reason, reason)
            self.assertNotIn('private', raised.exception.public_message)
            self.assertNotIn('secret', raised.exception.public_message)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, '', {}, 'https://else.invalid'))

    def test_cache_outside_workspace_is_rejected_without_network(self):
        net = Transport(payload())
        with self.assertRaises(LevelCatalogError) as raised:
            find_levels('TEST', Path(__file__).resolve().anchor, transport=net)
        self.assertEqual(raised.exception.reason, 'cache')
        self.assertEqual(net.calls, [])

    def test_explicit_data_root_supports_shared_profile_outside_frozen_install(self):
        profile = self.cache / 'shared-profile'
        directory = profile / 'research' / 'dispatch'
        install = self.cache / 'release' / 'app'
        net = Transport(payload())
        with patch('shu.level_catalog.WORKSPACE_ROOT', install):
            with self.assertRaises(LevelCatalogError) as raised:
                find_levels('TEST-7', directory, transport=net)
            self.assertEqual(raised.exception.reason, 'cache')
            self.assertEqual(net.calls, [])
            result = find_levels('TEST-7', directory, cache_root=profile, transport=net)
            cached = find_levels('TEST-7', directory, cache_root=profile,
                                 transport=Transport(RuntimeError('must use cache')))
        self.assertEqual(result['levels'][0]['stage_id'], 'test_internal_a')
        self.assertEqual(result['cache'], 'refreshed')
        self.assertEqual(cached['levels'], result['levels'])
        self.assertEqual(cached['cache'], 'hit')
        self.assertEqual(len(net.calls), 1)
        self.assertTrue((directory / 'level-catalog-cache.json').is_file())

    def test_explicit_data_root_does_not_authorize_sibling_cache_directory(self):
        net = Transport(payload())
        with self.assertRaises(LevelCatalogError) as raised:
            find_levels('TEST-7', self.cache / 'sibling', cache_root=self.cache / 'profile',
                        transport=net)
        self.assertEqual(raised.exception.reason, 'cache')
        self.assertEqual(net.calls, [])
        self.assertFalse((self.cache / 'sibling').exists())

    def test_derived_cache_files_cannot_escape_selected_directory(self):
        raw = payload()
        digest = hashlib.sha256(raw).hexdigest()
        for name, body in (
                ('level-catalog-cache.json', json.dumps({'source': 'https://prts.maa.plus/arknights/level',
                    'sha256': digest, 'fetched_at': time.time()}).encode()),
                (f'level-catalog-{digest}.json', raw)):
            with self.subTest(name=name):
                directory = self.cache / ('manifest' if name == 'level-catalog-cache.json' else 'raw')
                directory.mkdir()
                outside = self.cache / ('outside-' + name)
                outside.write_bytes(body)
                (directory / name).write_bytes(body)
                if name != 'level-catalog-cache.json':
                    (directory / 'level-catalog-cache.json').write_text(json.dumps({
                        'source': 'https://prts.maa.plus/arknights/level', 'sha256': digest,
                        'fetched_at': time.time()}), encoding='utf-8')
                net = Transport(raw)
                original_resolve = Path.resolve

                def resolve_link(path, *args, **kwargs):
                    # Windows cannot create file symlinks without a privilege.
                    # Simulate only that filesystem resolution; all cache IO,
                    # parsing, hashing and boundary validation remain real.
                    if path == directory / name:
                        return outside
                    return original_resolve(path, *args, **kwargs)

                with patch.object(Path, 'resolve', resolve_link):
                    with self.assertRaises(LevelCatalogError) as raised:
                        find_levels('TEST-7', directory, cache_root=self.cache, transport=net)
                self.assertEqual(raised.exception.reason, 'cache')
                self.assertEqual(outside.read_bytes(), body)
                self.assertEqual(len(net.calls), 1)

    def test_corrupt_cache_manifest_is_recovered_without_trusting_it(self):
        (self.cache / 'level-catalog-cache.json').write_text('[]', encoding='utf-8')
        result = find_levels('TEST-7', self.cache, transport=Transport(payload()))
        self.assertEqual(len(result['levels']), 2)
        self.assertEqual(result['cache'], 'refreshed')

    def test_tampered_raw_cache_is_replaced_only_by_valid_remote_metadata(self):
        raw = payload()
        find_levels('TEST-7', self.cache, transport=Transport(raw))
        digest = hashlib.sha256(raw).hexdigest()
        (self.cache / f'level-catalog-{digest}.json').write_bytes(payload([]))
        result = find_levels('TEST-7', self.cache, transport=Transport(raw))
        self.assertEqual(len(result['levels']), 2)
        self.assertEqual(result['cache'], 'refreshed')


if __name__ == '__main__':
    unittest.main()

"""Activity catalogs use invented offline levels, never a preselected live event."""
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu.level_catalog import LevelCatalogError, find_levels

try:
    activity_context = importlib.import_module('shu.activity_context')
except ModuleNotFoundError as exc:
    if exc.name != 'shu.activity_context':
        raise
    activity_context = None


def payload(rows):
    return json.dumps({'status_code': 200, 'data': rows}, ensure_ascii=False).encode('utf-8')


def level(code, *, event='虚构河岸', stage=None, name='离线关卡'):
    return {'stage_id': stage or 'offline_' + code.lower(), 'level_id': 'level_' + code.lower(),
            'cat_one': '活动关卡', 'cat_two': event, 'cat_three': code, 'name': name,
            'unknown_remote_field': 'not an instruction'}


class ActivityStagesTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(activity_context, 'activity_stages tool has not been implemented')
        base = Path(__file__).resolve().parents[2] / '.pet-temp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)

    def lookup(self, query, rows, **options):
        raw = payload(rows)
        return activity_context.activity_stages(query, self.cache,
            transport=lambda req, **kwargs: raw, **options)

    def artifact(self, result):
        ref = result['stage_catalog_ref']
        path = Path(ref['path'])
        self.assertTrue(path.is_relative_to(self.cache))
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ref['sha256'])
        return json.loads(raw)

    def test_activity_query_returns_ten_stages_and_complete_hashed_catalog(self):
        # Reusing the single-stage lookup's eight-result cap loses GH-9/GH-10.
        rows = [level('GH-' + str(i)) for i in range(1, 11)]
        result = self.lookup('虚构河岸', rows)
        self.assertTrue(result['ok'])
        self.assertEqual(result['tool'], 'activity_stages')
        self.assertEqual(result['event_name'], '虚构河岸')
        self.assertEqual([s['code'] for s in result['stages']],
                         ['GH-1', 'GH-2', 'GH-3', 'GH-4', 'GH-5', 'GH-6', 'GH-7', 'GH-8', 'GH-9', 'GH-10'])
        self.assertEqual(result['total_stages'], 10)
        self.assertEqual(result['returned_stages'], 10)
        self.assertFalse(result['has_more'])
        self.assertTrue(result['catalog_complete'])
        self.assertEqual(result['raw_sha256'], hashlib.sha256(payload(rows)).hexdigest())
        self.assertEqual(result['source'], 'https://prts.maa.plus/arknights/level')
        saved = self.artifact(result)
        self.assertEqual(saved['stages'], result['stages'])
        self.assertEqual(saved['event_name'], '虚构河岸')
        self.assertNotIn('unknown_remote_field', result['stages'][0])
        self.assertFalse(result['account_unlock_verified'])
        self.assertFalse(result['opening_status_verified'])
        self.assertFalse(result['content_is_instruction'])

    def test_arbitrary_other_activity_keeps_ex_s_and_duplicate_code_ids_without_opening_claims(self):
        # Prefix assumptions would reject a different activity or EX/S categories.
        result = self.lookup('另一假活动', [level('AN-1', event='另一假活动'),
            level('AN-EX-1', event='另一假活动'), level('AN-S-1', event='另一假活动'),
            level('AN-1', event='另一假活动', stage='offline_an_challenge'),
            level('ELSE-1', event='无关活动')])
        self.assertEqual(result['event_name'], '另一假活动')
        self.assertEqual([s['stage_id'] for s in result['stages']],
            ['offline_an-1', 'offline_an-ex-1', 'offline_an-s-1', 'offline_an_challenge'])
        self.assertEqual(result['total_stages'], 4)
        self.assertFalse(result['opening_status_verified'])
        self.assertNotIn('opened', result['stages'][0])

    def test_exact_category_wins_and_unique_substring_resolves_without_aliases(self):
        rows = [level('GH-1'), level('GH-EX-1', event='虚构河岸 复刻'),
                level('XZ-1', event='虚构雪原')]
        exact = self.lookup('虚构河岸', rows)
        self.assertEqual([s['code'] for s in exact['stages']], ['GH-1'])
        partial = self.lookup('雪原', rows)
        self.assertTrue(partial['ok'])
        self.assertEqual(partial['event_name'], '虚构雪原')
        self.assertEqual([s['code'] for s in partial['stages']], ['XZ-1'])

    def test_ambiguous_activity_does_not_silently_choose_first_matching_event(self):
        result = self.lookup('虚构', [level('GH-1'), level('XZ-1', event='虚构雪原')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'activity_ambiguous')
        self.assertEqual(result['event_name'], '')
        self.assertEqual(result['stages'], [])
        self.assertEqual(result['event_candidates'],
            [{'event_name': '虚构河岸', 'total_stages': 1}, {'event_name': '虚构雪原', 'total_stages': 1}])
        saved = self.artifact(result)
        self.assertEqual(saved['total_stages'], 2)
        self.assertEqual([s['code'] for s in saved['stages']], ['GH-1', 'XZ-1'])

    def test_missing_activity_is_not_matched_by_stage_code_or_name(self):
        result = self.lookup('GH-1', [level('GH-1')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'activity_not_found')
        self.assertEqual(result['total_stages'], 0)
        self.assertEqual(result['stages'], [])
        self.assertEqual(self.artifact(result)['stages'], [])

    def test_over_two_hundred_stages_remain_in_artifact_and_model_sees_explicit_truncation(self):
        rows = [level('ANY-' + str(i)) for i in range(1, 206)]
        result = self.lookup('虚构河岸', rows)
        self.assertTrue(result['ok'])
        self.assertEqual(result['total_stages'], 205)
        self.assertEqual(result['returned_stages'], 200)
        self.assertTrue(result['has_more'])
        self.assertFalse(result['catalog_complete'])
        self.assertEqual(len(self.artifact(result)['stages']), 205)

    def test_activity_and_single_stage_lookup_share_checked_metadata_cache(self):
        calls = []
        def transport(req, **kwargs):
            calls.append(req.full_url)
            return payload([level('GH-1')])
        find_levels('GH-1', self.cache, transport=transport)
        result = activity_context.activity_stages('虚构河岸', self.cache,
            transport=lambda *a, **kw: self.fail('valid metadata cache was ignored'))
        self.assertEqual(result['cache'], 'hit')
        self.assertEqual(calls, ['https://prts.maa.plus/arknights/level'])
        self.assertEqual(result['stages'][0]['stage_id'], 'offline_gh-1')

    def test_external_authorized_profile_and_invalid_queries_preserve_boundaries(self):
        profile = self.cache / 'profile'
        directory = profile / 'research'
        with patch('shu.level_catalog.WORKSPACE_ROOT', self.cache / 'frozen-install'):
            result = activity_context.activity_stages('虚构河岸', directory,
                cache_root=profile, transport=lambda *a, **kw: payload([level('GH-1')]))
            self.assertTrue(result['ok'])
            with self.assertRaises(LevelCatalogError) as raised:
                activity_context.activity_stages('虚构河岸', self.cache / 'sibling', cache_root=profile,
                    transport=lambda *a, **kw: self.fail('unauthorized request'))
            self.assertEqual(raised.exception.reason, 'cache')
        for query in ('', 'https://else.invalid', 'Bearer private', 'x\ny'):
            with self.subTest(query=query), self.assertRaises(LevelCatalogError) as raised:
                activity_context.activity_stages(query, self.cache,
                    transport=lambda *a, **kw: self.fail('invalid query reached network'))
            self.assertEqual(raised.exception.reason, 'query')

    def test_stage_catalog_symlink_cannot_write_outside_selected_cache(self):
        # Catalog hashing must never turn an existing redirected path into a write.
        first = self.lookup('虚构河岸', [level('GH-1')])
        catalog = Path(first['stage_catalog_ref']['path'])
        outside = self.cache / 'outside' / catalog.name
        outside.parent.mkdir()
        outside.write_bytes(b'preserve outside data')
        original_resolve = Path.resolve
        # Place the selected cache below a different boundary, so the redirected
        # catalog is truly outside that selected directory.
        directory = self.cache / 'chosen'
        target = directory / catalog.name
        def resolve_chosen(path, *args, **kwargs):
            if path == target:
                return outside
            return original_resolve(path, *args, **kwargs)
        with patch.object(Path, 'resolve', resolve_chosen):
            with self.assertRaises(LevelCatalogError) as raised:
                activity_context.activity_stages('虚构河岸', directory,
                    transport=lambda *a, **kw: payload([level('GH-1')]))
        self.assertEqual(raised.exception.reason, 'cache')
        self.assertEqual(outside.read_bytes(), b'preserve outside data')

    def test_oversized_remote_category_is_saved_but_cannot_flood_or_resolve_model_output(self):
        event = '虚构' + '远程文本' * 2000
        result = self.lookup('虚构', [level('GH-1', event=event)])
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'activity_label_unsupported')
        self.assertFalse(result['catalog_complete'])
        self.assertEqual(result['event_name'], '')
        self.assertEqual(result['stages'], [])
        self.assertTrue(result['event_candidates'][0]['name_truncated'])
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 2000)
        self.assertEqual(self.artifact(result)['event_candidates'][0]['event_name'], event)


if __name__ == '__main__':
    unittest.main()

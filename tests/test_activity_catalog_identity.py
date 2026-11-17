"""Full local references preserve public identity across preview pages."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shu.activity_context import activity_stages, activity_catalog_page, read_activity_catalog
from shu.activity_structure import normalize_structure
from shu.level_catalog import LevelCatalogError, find_levels


class CatalogIdentityTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '.pet-temp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def lookup(self, rows):
        raw = json.dumps({'status_code': 200, 'data': rows}).encode()
        return activity_stages('Test', self.root, transport=lambda *a, **k: raw)

    def test_paged_preview_does_not_truncate_authoritative_catalog(self):
        rows = [{'stage_id': 'i'+str(i), 'level_id': i, 'cat_two': 'Test', 'cat_three': 'T-'+str(i)}
                for i in range(230)]
        preview = self.lookup(rows)
        self.assertTrue(preview['has_more'])
        self.assertEqual(normalize_structure(preview, {})['code'], 'catalog_truncated')
        ref = preview['stage_catalog_ref']
        full = read_activity_catalog(ref, cache_root=self.root)
        self.assertEqual(len(normalize_structure(full, {})['stages']), 230)
        page = activity_catalog_page(ref, cache_root=self.root, offset=200, limit=50)
        self.assertEqual(len(page['stages']), 30)
        self.assertIsNone(page['next_offset'])
        with self.assertRaises(LevelCatalogError):
            activity_catalog_page(ref, cache_root=self.root, limit=201)
        Path(ref['path']).write_bytes(b'{}')
        with self.assertRaises(LevelCatalogError):
            read_activity_catalog(ref, cache_root=self.root)

    def test_same_stage_and_level_difficulty_and_branch_not_dropped(self):
        rows = [{'stage_id': 'opaque', 'level_id': 'same', 'cat_two': 'Test', 'cat_three': 'T-1',
                 'difficulty': d, 'branch': branch, 'query_key': 'q'}
                for d, branch in [(1, 'A'), (2, 'A'), (1, 'B')]]
        result = self.lookup(rows)
        self.assertEqual(len(result['stages']), 3)
        self.assertEqual({(s['difficulty'], s['branch']) for s in result['stages']},
                         {(1, 'A'), (2, 'A'), (1, 'B')})
        self.assertTrue(all(s['query_key'] == 'q' and s['source_refs'] for s in result['stages']))
        found = find_levels('T-1', self.root)
        self.assertEqual(found['total_matches'], 3)

    def test_read_reference_hash_and_directory_are_both_required(self):
        result = self.lookup([{'stage_id': 'i', 'level_id': 1, 'cat_two': 'Test'}])
        ref = result['stage_catalog_ref']
        with self.assertRaises(LevelCatalogError):
            read_activity_catalog(dict(ref, sha256='0'*64), cache_root=self.root)
        with self.assertRaises(LevelCatalogError):
            read_activity_catalog(ref, cache_root=self.root / 'other')


if __name__ == '__main__':
    unittest.main()

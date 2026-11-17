"""Offline checks for the official-manual importer; never access game or keys."""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from shu.knowledge import KnowledgeStore

try:
    from tools import import_maa_manual as manual
except ImportError:
    manual = None


class MaaManualImportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(manual, 'The bounded MAA manual importer must exist')
        # This Windows host cannot atomically replace files through its 8.3
        # system-temp alias. Exercise the real atomic writer inside the project.
        temporary_root = Path(__file__).resolve().parents[1] / '.test-temp'
        temporary_root.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=temporary_root)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def fixture(self):
        root = self.root / 'manual'
        root.mkdir()
        payload = '# 自动战斗\n\n作业导入支持自动编队。PC 客户端连接说明。\n'
        path = root / 'manual' / 'combat.md'
        path.parent.mkdir()
        data = payload.encode()
        path.write_bytes(data)
        manifest = {
            'schema_version': 1, 'repository': manual.REPOSITORY,
            'version': manual.VERSION, 'commit': manual.COMMIT,
            'source_updated_at': '2026-11-04T09:48:42Z',
            'documents': [{
                'path': 'manual/combat.md', 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'source_url': manual.source_url('manual/combat.md'),
            }], 'failures': [], 'discovered_markdown': 1,
        }
        (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return root

    def test_paths_cannot_escape_or_use_windows_drive_or_links(self):
        for path in ('../outside.md', '/absolute.md', 'C:/outside.md',
                     'nested/../../outside.md', 'nested\\outside.md',
                     'a//b.md', 'a/./b.md', 'a/NUL.md', 'x.md:stream'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manual.checked_destination(self.root, path)

    def test_chunks_preserve_whole_document_and_fit_store_limit(self):
        text = '# 自动战斗\n\n' + ('自动编队及连接说明。\n' * 1500)
        chunks = list(manual.chunk_markdown(text))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(0 < len(chunk) <= 4000 for chunk in chunks))

    def test_import_is_repeatable_preserves_preferences_and_keeps_backup(self):
        docs = self.fixture()
        profile = self.root / 'profile'
        store = KnowledgeStore(profile)
        preference = store.add_preference('服药偏好', '使用理智药前先问我。')
        store.close()
        report = manual.import_documents(docs, profile)
        self.assertEqual(report['imported_chunks'], 1)
        self.assertEqual(report['import_failures'], [])
        self.assertTrue(report['existing_entries_preserved'])
        self.assertTrue(Path(report['backup']).is_file())
        with closing(sqlite3.connect(report['backup'])) as snapshot:
            self.assertEqual(snapshot.execute('SELECT COUNT(*) FROM knowledge_entries').fetchone()[0], 1)
        second = manual.import_documents(docs, profile)
        self.assertNotEqual(second['backup'], report['backup'])
        store = KnowledgeStore(profile)
        self.addCleanup(store.close)
        self.assertEqual(store.preferences()[0]['id'], preference['id'])
        for query in ('自动战斗', '作业', '导入', '自动编队', 'PC', '连接'):
            hits = store.retrieve(query, source_kinds={'reference'})
            self.assertTrue(hits, query)
            self.assertFalse(hits[0]['verified'])
            self.assertEqual(hits[0]['evidence_status'], 'source_reported')
            self.assertIn(manual.COMMIT, hits[0]['version'])
        with closing(sqlite3.connect(profile / 'knowledge.sqlite3')) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM knowledge_entries').fetchone()[0], 2)

    def test_hash_mismatch_stops_before_touching_existing_database(self):
        docs = self.fixture()
        (docs / 'manual' / 'combat.md').write_text('changed', encoding='utf-8')
        profile = self.root / 'profile'
        with self.assertRaises(ValueError):
            manual.import_documents(docs, profile)
        self.assertFalse(profile.exists())


if __name__ == '__main__':
    unittest.main()

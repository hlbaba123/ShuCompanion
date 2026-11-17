"""Explicit, durable source preferences without caching current event facts."""
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import closing
import importlib
import unittest

from shu.knowledge import KnowledgeStore


class SourcePolicyTests(unittest.TestCase):
    def test_user_sources_survive_restart_as_one_nonexpiring_attributed_preference(self):
        try:
            policy = importlib.import_module('shu.source_policy')
        except ModuleNotFoundError:
            self.fail('explicit user source-policy persistence is missing')
        with TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory), clock=lambda: 1000.0)
            first = policy.install_user_source_preferences(store)
            policy.install_user_source_preferences(store)
            store.close()
            with closing(KnowledgeStore(Path(directory), clock=lambda: 1000.0 + 20 * 365 * 86400)) as reopened:
                rows = reopened.preferences(top_k=20, char_budget=10000, token_budget=10000)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['id'], first['id'])
                self.assertEqual(rows[0]['source_kind'], 'user_preference')
                self.assertEqual(rows[0]['evidence_status'], 'user_stated')
                self.assertIsNone(rows[0]['expires_at'])
                self.assertFalse(rows[0]['stale'])
                self.assertIn('https://prts.plus/', rows[0]['text'])
                self.assertIn('https://prts.wiki/', rows[0]['text'])
                self.assertNotIn('正在开放', rows[0]['text'])
                self.assertNotIn('关卡列表', rows[0]['text'])


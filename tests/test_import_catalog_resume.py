"""Continue pins this task revision's catalog without model or GUI activity."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from shu.maa_agents import MaaRoleAgents
from shu.planned_agents import register_import_plan, verify_planned_completion
from shu.leader_plan import read_reference
from tests import test_split_import_scope as scope_fixtures


class ImportCatalogResumeTests(unittest.TestCase):
    def setUp(self):
        fixture = self.fixture = scope_fixtures.SplitImportScopeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.store = fixture.root, fixture.store
        self.task = fixture.task(scope_fixtures.import_contract(scope_fixtures.scope('all')))
        self.raw = {'event_name': '测试活动', 'source': 'https://prts.maa.plus/arknights/level',
            'raw_sha256': 'a'*64, 'catalog_complete': True, 'has_more': False,
            'total_stages': len(fixture.stages), 'stages': fixture.stages}
        self.catalog_ref = self.save('raw-catalog.json', self.raw)
        self.structure = {**fixture.structure, 'catalog_ref': self.catalog_ref}
        self.structure_ref = self.save('pinned-structure.json', self.structure)
        self.dispatch = self.store.ensure_dispatch(self.task['task_id'], 'catalog', 'original-catalog', {})
        result = {**self.raw, 'ok': True, 'stage_catalog_ref': self.catalog_ref,
                  'allow_start': True, 'writes_enabled': True}
        self.observation = {'task_id': self.task['task_id'], 'epoch': self.task['epoch'],
            'dispatch_id': self.dispatch['dispatch_id'], 'read_only': True,
            'tool': 'research_activity_stages', 'result': result}
        self.observation_ref = self.save('original-observation.json', self.observation)
        receipt = {'status': 'completed', 'structure_ref': self.structure_ref,
                   'evidence_refs': [self.observation_ref]}
        self.store.finish_dispatch(self.dispatch['dispatch_id'], receipt)
        state = {**self.task, 'role_results': {'catalog': receipt}}
        self.plan = register_import_plan(self.store, state, self.task['contract'])
        self.store.control(self.task['task_id'], 'pause')
        resumed = self.store.control(self.task['task_id'], 'resume')
        current_dispatch = self.store.ensure_dispatch(self.task['task_id'], 'catalog', 'resumed-catalog', {})
        self.context = {**resumed, 'dispatch_id': current_dispatch['dispatch_id']}
        self.agents = MaaRoleAgents.__new__(MaaRoleAgents)
        self.agents.store = self.store
        self.agents.directory = self.root
        self.agents._closed = False
        self.agents.activity_callback = None
        # No clients, input backend or supervisor is supplied. Any accidental
        # model/GUI path therefore fails this integration test.

    def save(self, name, value):
        path = self.root / name
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def test_same_revision_uses_original_edition_without_network_or_model(self):
        result = self.agents._handler('catalog')(self.context, threading.Event())
        self.assertEqual(result['code'], 'pinned_catalog_revalidated')
        self.assertEqual(result['structure_ref'], self.structure_ref)
        self.assertNotEqual(self.context['epoch'], self.dispatch['epoch'])
        for reference in result['evidence_refs']:
            observation = read_reference(reference, self.root)
            self.assertEqual(observation['epoch'], self.context['epoch'])
            self.assertEqual(observation['dispatch_id'], self.context['dispatch_id'])
            self.assertEqual(observation['revalidated_from']['observation_ref'], self.observation_ref)
            self.assertNotIn('allow_start', observation['result'])
            self.assertNotIn('writes_enabled', observation['result'])
        state = {**self.context, 'role_results': {'catalog': result}}
        self.assertTrue(verify_planned_completion(self.store, state, 'stage_catalog')['complete'])
        self.assertEqual(register_import_plan(self.store, state, state['contract']), self.plan)
        self.assertEqual(self.fixture.engine._db.execute('SELECT COUNT(*) FROM workflow_operations').fetchone()[0], 0)

    def test_tampered_catalog_or_structure_blocks_without_model_fallback(self):
        for reference in (self.catalog_ref, self.structure_ref, self.observation_ref):
            path = Path(reference['path'])
            original = path.read_bytes()
            try:
                path.write_bytes(b'{}')
                with self.assertRaises((ValueError, KeyError)):
                    self.agents._handler('catalog')(self.context, threading.Event())
            finally:
                path.write_bytes(original)

    def test_new_revision_does_not_adopt_previous_revision_catalog(self):
        changed = self.store.replace_contract(self.task['task_id'], '新版本要求', self.task['contract'],
            expected_revision=self.context['revision'], expected_epoch=self.context['epoch'])
        context = {**changed, 'dispatch_id': 'not-used'}
        self.assertIsNone(self.agents._pinned_import_catalog(context, threading.Event()))

    def test_bad_source_dispatch_identity_blocks(self):
        bad = copy.deepcopy(self.observation)
        bad['dispatch_id'] = 'different-dispatch'
        reference = self.save('wrong-dispatch.json', bad)
        from shu.workflow_store import _encode
        payload, digest = _encode({'status': 'completed', 'structure_ref': self.structure_ref,
                                  'evidence_refs': [reference]})
        self.fixture.engine._db.execute('UPDATE workflow_results SET result=?,payload_hash=? WHERE dispatch_id=?',
            (payload, digest, self.dispatch['dispatch_id']))
        with self.assertRaises(ValueError):
            self.agents._pinned_import_catalog(self.context, threading.Event())


class RecoveryArtifactPathTests(unittest.TestCase):
    def test_short_capture_kind_fits_original_260_character_case(self):
        from shu.maa_queue_receipt import _publish
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            padding = 163 - len(str(root)) - 1
            self.assertGreater(padding, 0)
            directory = root / ('x' * padding)
            reference = _publish(directory, 'maa-queue-rec-capture', {'fixture': 'capture-provenance'})
            self.assertLess(len(reference['path']), 260)
            self.assertTrue(Path(reference['path']).is_file())


if __name__ == '__main__':
    unittest.main()

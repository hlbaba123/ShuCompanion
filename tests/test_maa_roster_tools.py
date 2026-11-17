"""Local MAA export bridge and real matching, without model or native GUI."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shu.core import TaskEngine
from shu.workflow_store import WorkflowStore
from shu.maa_roster_tools import RosterTools


class RosterToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.tools = RosterTools(WorkflowStore(self.engine), self.root)

    def write_export(self, complete):
        path = self.root / 'roster-imports' / 'maa-roster.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'done': complete,
            'all_opers': [{'id': 'char_fixture', 'name': 'Fixture', 'own': True, 'rarity': 6}],
            'own_opers': [{'id': 'char_fixture', 'name': 'Fixture', 'own': True,
                           'elite': 2, 'level': 80, 'potential': 1, 'rarity': 6}]}))
        return path

    def copilot(self):
        path = self.root / 'research' / 'job.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'stage_name': 'fixture_stage', 'actions': [{'type': 'Deploy'}],
            'opers': [{'name': 'Fixture', 'skill': 3,
                       'requirements': {'elite': 2, 'level': 60, 'skill_level': 10, 'module': 1}}]}))
        return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_no_box_is_unknown_not_satisfied(self):
        result = self.tools.assess('profile-A', self.copilot())
        self.assertEqual(result['classification'], 'unknown')
        self.assertIn('roster', str(result['missing_requirements']))

    def test_real_maa_export_shared_and_missing_mastery_remains_unknown(self):
        self.write_export(True)
        first = self.tools.import_export('profile-A')
        other = RosterTools(self.tools.store, self.root)
        self.assertEqual(other.current('profile-A')['box_revision'], first['box_revision'])
        result = other.assess('profile-A', self.copilot(),
                              {'ignore_level': False, 'ignore_skill': False, 'ignore_module': False})
        self.assertEqual(result['classification'], 'unknown')
        self.assertTrue(result['missing_requirements'])

    def test_changed_document_or_outside_reference_rejected(self):
        ref = self.copilot()
        Path(ref['path']).write_text('{}')
        with self.assertRaises(ValueError):
            self.tools.assess('profile-A', ref)
        with self.assertRaises(ValueError):
            self.tools.assess('profile-A', {'path': str(self.root.parent / 'external.json'), 'sha256': '0' * 64})

    def test_partial_export_cannot_claim_complete_account(self):
        self.write_export(False)
        result = self.tools.import_export('profile-A')
        self.assertFalse(result['complete'])
        self.assertEqual(result['profile_id'], 'profile-A')

    def test_no_export_does_not_create_fictitious_roster(self):
        with self.assertRaises(FileNotFoundError):
            self.tools.import_export('profile-A')
        self.assertIsNone(self.tools.current('profile-A'))


if __name__ == '__main__':
    unittest.main()

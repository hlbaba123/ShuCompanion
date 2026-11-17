"""Account-owned roster screening from stored public metadata, without downloads."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shu.core import TaskEngine
from shu.copilot_pool import CopilotPool
from shu.maa_roster_tools import RosterTools


class RosterScreeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.tools = RosterTools(self.engine, self.root)
        self.source = {'kind': 'user-roster-spreadsheet', 'observed_at': 1000,
                       'evidence_ref': 'fixture:account.xlsx#sha256=abc'}

    def roster(self, *operators, complete=False):
        return self.tools.facts.publish('local-default', list(operators), complete,
                                        self.source, explicit_ownership=True)

    def candidate(self, identifier, *operators, groups=()):
        return {'id': identifier, 'source': f'https://prts.maa.plus/copilot/get/{identifier}',
                'mystery_code': f'prts://{identifier}', 'stage_name': 'unit_stage',
                'title': f'Candidate {identifier}', 'operators': list(operators),
                'groups': list(groups), 'requires_download': True, 'available': True}

    def pool(self, candidates):
        pool = {'schema': 1, 'pool_id': CopilotPool.identity('unit_stage', None),
                'stage_id': 'unit_stage', 'document': None, 'candidates': candidates,
                'coverage': {'collected': len(candidates), 'total': len(candidates),
                             'complete': True, 'next_page': None}, 'sources': ['fixture:catalog']}
        return CopilotPool(self.root / 'task-cache')._save(pool)

    def screen(self, candidates, policy=None):
        self.assertTrue(callable(getattr(self.tools, 'screen_pool', None)),
                        'RosterTools must bulk screen the persisted pool')
        result = self.tools.screen_pool('local-default', self.pool(candidates), policy)
        ref = result['screen_ref']
        raw = Path(ref['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ref['sha256'])
        return result, json.loads(raw)

    def test_bulk_screen_preserves_all_results_and_limits_model_projection(self):
        box = self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2})
        result, proof = self.screen([self.candidate(i, {'name': '甲'}) for i in range(1, 28)])
        self.assertEqual(result['counts']['satisfied'], 27)
        self.assertEqual(len(result['accepted']), 10)
        self.assertEqual(len(proof['results']), 27)
        self.assertEqual(result['roster_ref']['batch_id'], box['batch_id'])
        self.assertEqual(result['box_revision'], box['box_revision'])
        self.assertEqual(result['policy_sha256'], proof['policy_sha256'])
        self.assertTrue(result['requires_full_document_verification'])
        self.assertFalse(result['can_start_battle'])

    def test_default_policy_requires_high_rarity_elite_two_even_without_document_minimum(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 1})
        result, proof = self.screen([self.candidate(1, {'name': '甲'})])
        self.assertEqual(result['counts']['unsatisfied'], 1)
        self.assertEqual(result['accepted'], [])
        self.assertEqual(proof['results'][0]['missing_requirements'][0]['field'], 'elite')
        self.assertEqual(proof['results'][0]['hard_elite_checks'][0]['required'], 2)

    def test_ownership_is_checked_before_elite_and_single_missing_support_stays_conditional(self):
        self.roster({'name': '甲', 'owned': False, 'rarity': 6, 'elite': 0})
        result, proof = self.screen([self.candidate(1, {'name': '甲'}),
                                     self.candidate(2, {'name': '表外'})])
        self.assertEqual(result['counts']['conditional'], 1)
        self.assertEqual(result['counts']['unknown'], 1)
        self.assertEqual(result['accepted'], [])
        self.assertEqual(proof['results'][0]['ownership_checks'][0]['state'], 'explicit_absent')
        self.assertEqual(proof['results'][1]['ownership_checks'][0]['state'], 'unknown')

    def test_two_missing_operators_cannot_be_repaired_with_two_borrows(self):
        self.roster({'name': '甲', 'owned': False}, {'name': '乙', 'owned': False})
        result, _ = self.screen([self.candidate(1, {'name': '甲'}, {'name': '乙'})])
        self.assertEqual(result['counts']['unsatisfied'], 1)

    def test_user_policy_ignores_only_level_skill_and_module_thresholds_with_audit(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 5, 'elite': 2, 'level': 1})
        requirement = {'name': '甲', 'skill': 3, 'requirements': {
            'elite': 1, 'level': 90, 'skill_level': 10, 'module': 1, 'module_level': 3}}
        result, proof = self.screen([self.candidate(1, requirement)])
        self.assertEqual(result['counts']['satisfied'], 1)
        ignored = proof['results'][0]['ignored_requirements']
        self.assertEqual({x['field'] for x in ignored}, {'level', 'skill_level', 'module', 'module_level'})
        self.assertTrue(all(x['reason'] == 'user_policy' for x in ignored))
        self.assertEqual(proof['results'][0]['hard_elite_checks'][0]['required'], 2)

    def test_potentiality_alias_remains_a_hard_requirement(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2, 'potential': 1})
        result, proof = self.screen([self.candidate(1, {'name': '甲',
                                                       'requirements': {'potentiality': 2}})])
        self.assertEqual(result['counts']['unsatisfied'], 1)
        self.assertEqual(proof['results'][0]['missing_requirements'][0]['field'], 'potential')

    def test_unknown_future_requirements_are_retained_and_prevent_acceptance(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2})
        result, proof = self.screen([self.candidate(1, {'name': '甲',
            'requirements': {'future_module_id': {'minimum': 1}}})])
        self.assertEqual(result['counts']['unknown'], 1)
        missing = proof['results'][0]['missing_requirements']
        self.assertEqual(missing[0]['actual'], {'future_module_id': {'minimum': 1}})

    def test_group_alternatives_are_choices_and_share_operator_identity_across_slots(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2},
                    {'name': '乙', 'owned': True, 'rarity': 4, 'elite': 1})
        group = {'name': '任选治疗', 'opers': [{'name': '甲'}, {'name': '乙'}, {'name': '丙'}]}
        result, proof = self.screen([self.candidate(1, {'name': '甲'}, groups=[group])])
        self.assertEqual(result['counts']['satisfied'], 1)
        self.assertEqual([x['name'] for x in proof['results'][0]['assignments']], ['甲', '乙'])

    def test_missing_rarity_cannot_silently_waive_elite_floor(self):
        self.roster({'name': '甲', 'owned': True, 'elite': 1})
        result, proof = self.screen([self.candidate(1, {'name': '甲'})])
        self.assertEqual(result['counts']['unknown'], 1)
        self.assertEqual(proof['results'][0]['missing_requirements'][0]['field'], 'rarity')

    def test_policy_cannot_disable_elite_checks(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 1})
        self.assertTrue(callable(getattr(self.tools, 'screen_pool', None)))
        with self.assertRaises(ValueError):
            self.tools.screen_pool('local-default', self.pool([self.candidate(1, {'name': '甲'})]),
                                   {'ignore_elite': True})

    def test_screen_ref_must_resolve_under_app_directory_and_verify_hash(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2})
        self.assertTrue(callable(getattr(self.tools, 'screen_pool', None)))
        ref = self.pool([self.candidate(1, {'name': '甲'})])
        Path(ref['path']).write_text('{}')
        with self.assertRaises(ValueError):
            self.tools.screen_pool('local-default', ref)

    def test_full_document_assessment_reapplies_same_policy_and_publishes_source_proof(self):
        box = self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2})
        requirement = {'name': '甲', 'requirements': {'elite': 2, 'level': 90, 'skill_level': 10}}
        screened, _ = self.screen([self.candidate(1, requirement)])
        path = self.root / 'job.json'
        path.write_text(json.dumps({'stage_name': 'unit_stage', 'opers': [requirement],
                                    'actions': [{'type': 'Deploy'}]}))
        ref = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        assessed = self.tools.assess('local-default', ref)
        self.assertEqual(assessed['classification'], 'satisfied')
        self.assertEqual(assessed['policy_sha256'], screened['policy_sha256'])
        self.assertEqual(assessed['roster_ref']['batch_id'], box['batch_id'])
        self.assertEqual(assessed['copilot_ref'], ref)
        self.assertEqual({x['field'] for x in assessed['ignored_requirements']}, {'level', 'skill_level'})

    def test_unknown_requirements_remain_unknown_even_when_operator_is_absent(self):
        self.roster({'name': '甲', 'owned': False})
        result, proof = self.screen([self.candidate(1, {'name': '甲',
            'requirements': {'future_field': {'minimum': 2}}})])
        self.assertEqual(result['counts']['unknown'], 1)
        self.assertIn('unsupported_requirements',
                      {fact['field'] for fact in proof['results'][0]['missing_requirements']})

    def test_independent_tasks_cannot_smuggle_public_roster_into_user_facts(self):
        result, proof = self.screen([self.candidate(1, {'name': '甲',
            'owned': True, 'rarity': 6, 'elite': 2})])
        self.assertEqual(result['counts']['unknown'], 1)
        self.assertIsNone(self.tools.current('local-default'))
        self.assertEqual(proof['results'][0]['missing_requirements'][0]['field'], 'roster')

    def test_screen_snapshot_is_repeatable_and_keeps_earlier_roster_revision(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2})
        ref = self.pool([self.candidate(1, {'name': '甲'})])
        self.assertTrue(callable(getattr(self.tools, 'screen_pool', None)))
        first = self.tools.screen_pool('local-default', ref)
        repeat = self.tools.screen_pool('local-default', ref)
        self.assertEqual(first['screen_ref'], repeat['screen_ref'])
        self.source = dict(self.source, observed_at=1100, evidence_ref='fixture:update')
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 1})
        second = self.tools.screen_pool('local-default', ref)
        self.assertEqual(second['counts']['unsatisfied'], 1)
        self.assertNotEqual(first['screen_ref']['sha256'], second['screen_ref']['sha256'])
        self.assertEqual(json.loads(Path(first['screen_ref']['path']).read_bytes())['counts']['satisfied'], 1)

    def test_summary_omission_does_not_disappear_from_full_document_recheck(self):
        self.roster({'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2},
                    {'name': '乙', 'owned': True, 'rarity': 5, 'elite': 1})
        screened, _ = self.screen([self.candidate(1, {'name': '甲'})])
        self.assertEqual(screened['accepted_count'], 1)
        path = self.root / 'different-job.json'
        path.write_text(json.dumps({'stage_name': 'unit_stage',
            'opers': [{'name': '甲'}, {'name': '乙'}], 'actions': [{'type': 'Deploy'}]}))
        assessed = self.tools.assess('local-default', {
            'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        self.assertEqual(assessed['classification'], 'unsatisfied')

    def test_one_real_fresh_support_is_distinct_from_unverified_borrow_placeholder(self):
        from shu.roster_match import assess_requirements
        box = self.roster({'name': '甲', 'owned': False, 'rarity': 6})
        support = {'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2,
                   'verified': True, 'profile_id': 'local-default', 'observed_at': 1000,
                   'source': 'fixture:actual-support-list', 'evidence_ref': 'fixture:support-screenshot'}
        result = assess_requirements({'opers': [{'name': '甲'}]}, box,
                                     supports=[support], now=1100)
        self.assertEqual(result['classification'], 'needs_verified_support')
        self.assertEqual(result['support_status'], 'verified_available')
        self.assertEqual(result['hard_elite_checks'][0]['actual'], 2)
        self.assertEqual(result['support_evidence'][0]['evidence_ref'], 'fixture:support-screenshot')
        stale = assess_requirements({'opers': [{'name': '甲'}]}, box,
                                    supports=[support], now=1400)
        self.assertEqual(stale['classification'], 'conditional')
        self.assertEqual(stale['support_status'], 'verification_required')


if __name__ == '__main__':
    unittest.main()

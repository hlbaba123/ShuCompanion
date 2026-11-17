"""Offline identity and evidence contracts; historical snapshots are public facts."""
import copy
import json
from pathlib import Path
import unittest

from shu.activity_structure import normalize_structure, ordered_stage_keys


def catalog():
    return {'event_name': '离线群岛', 'source': 'https://example.invalid/catalog',
            'raw_sha256': 'a' * 64, 'catalog_complete': True, 'stages': [
        {'stage_id': f'id-{code}', 'level_id': code, 'code': code,
         'event': '离线群岛', 'difficulty': difficulty, 'branch': branch}
        for code, difficulty, branch in [('X-10', 1, ''), ('X-2-A', 1, 'A'),
                                         ('X-2-B', 1, 'B'), ('X-1', 1, '')]]}


class StructureTests(unittest.TestCase):
    def test_historical_sn_preserves_all_36_rows_and_unknown_difficulty(self):
        path = Path(__file__).resolve().parents[1] / 'artifacts/20261107-leader-redesign/catalog-scope/public-catalog-observations.json'
        fixture = json.loads(path.read_text(encoding='utf-8'))['愚人号']
        result = normalize_structure(fixture, {})
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['stages']), 36)
        self.assertEqual(len({s['display_code'] for s in result['stages']}), 28)
        aliases = [s for s in result['stages'] if s['display_code'] == 'SN-EX-1']
        self.assertEqual(len(aliases), 2)
        self.assertEqual({s['difficulty'] for s in aliases}, {0})
        self.assertEqual({s['query_key'] for s in aliases}, {s['stage_id'] for s in aliases})
        self.assertTrue(all(not s['query_key_verified'] for s in aliases))
        self.assertEqual(len(set(ordered_stage_keys(result))), 36)

    def test_no_invented_groups_and_natural_branch_order(self):
        result = normalize_structure(catalog(), {})
        self.assertTrue(result['ok'], result)
        self.assertEqual([g['label'] for g in result['groups']], ['离线群岛'])
        by_key = {s['stage_key']: s for s in result['stages']}
        self.assertEqual([by_key[k]['display_code'] for k in ordered_stage_keys(result)],
                         ['X-1', 'X-2-A', 'X-2-B', 'X-10'])
        self.assertTrue(all(s['phase_id'] is None and s['navigation_region_id'] is None
                            and not s['account_unlock_verified'] for s in result['stages']))

    def test_dynamic_groups_evidence_unknown_phase_and_cross_region(self):
        data = catalog()
        data['groups'] = [{'group_id': 'harbor', 'label': '港口',
                           'stage_ids': [s['stage_id'] for s in data['stages']],
                           'source_refs': [{'source': 'https://example.invalid/map'}]}]
        data['stages'][0]['navigation_region_id'] = 'east'
        data['stages'][1]['navigation_region_id'] = 'west'
        proposal = {'groups': copy.deepcopy(data['groups']),
                    'unknown_phase_links': [{'phase_id': 'later', 'source_refs': [{'source': 'opening'}]}]}
        result = normalize_structure(data, proposal)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['unknown_phase_links'], proposal['unknown_phase_links'])
        self.assertEqual(result['stages'][0]['navigation_region_id'], 'east')
        self.assertEqual(len(result['groups']), 1)

    def test_reject_fake_group_missing_members_and_unknown_members(self):
        data = catalog()
        for proposal, expected in [
            ({'groups': [{'group_id': 'fake', 'label': 'EX', 'stage_ids': ['id-X-1']}]}, 'unverified_group'),
            ({'groups': [{'group_id': 'partial', 'label': '离线群岛', 'stage_ids': ['id-X-1']}]}, 'missing_members'),
            ({'groups': [{'group_id': 'all', 'label': '离线群岛', 'stage_ids': ['absent']}]}, 'unknown_stage_ids')]:
            with self.subTest(expected=expected):
                self.assertEqual(normalize_structure(data, proposal)['code'], expected)

    def test_same_code_different_difficulty_and_ambiguous_stage_id(self):
        data = catalog()
        data['stages'] = [{'stage_id': 'same', 'level_id': 'l', 'code': 'X-1', 'difficulty': n}
                          for n in [1, 2]]
        result = normalize_structure(data, {})
        self.assertEqual(len(set(ordered_stage_keys(result))), 2)
        self.assertEqual(normalize_structure(data, {'groups': [{'group_id': 'a', 'label': '离线群岛',
                         'stage_ids': ['same']}]})['code'], 'difficulty_ambiguous')

    def test_missing_and_truncated_catalog(self):
        self.assertEqual(normalize_structure({}, {})['code'], 'missing_catalog')
        data = catalog()
        data['has_more'] = True
        self.assertEqual(normalize_structure(data, {})['code'], 'catalog_truncated')

    def test_evidenced_query_mapping_does_not_erase_identity(self):
        data = catalog()
        data['stages'] = [{'stage_id': 'opaque#f#', 'level_id': 'l', 'code': 'X-1'}]
        result = normalize_structure(data, {'query_mappings': [{'stage_id': 'opaque#f#',
            'query_key': 'opaque', 'difficulty': 2, 'source_refs': [{'source': 'https://example.invalid/protocol'}]}]})
        stage = result['stages'][0]
        self.assertEqual((stage['stage_id'], stage['query_key'], stage['difficulty']), ('opaque#f#', 'opaque', 2))
        self.assertTrue(stage['query_key_verified'])
        self.assertEqual(normalize_structure(data, {'query_mappings': [{'stage_id': 'opaque#f#',
            'query_key': 'opaque', 'difficulty': 2}]})['code'], 'missing_evidence')

    def test_predecessors_and_explicit_exclusions_must_be_evidenced(self):
        data = catalog()
        proposal = {'order_constraints': [{'before': 'id-X-10', 'after': 'id-X-1',
                         'source_refs': [{'source': 'https://example.invalid/order'}]}]}
        result = normalize_structure(data, proposal)
        rows = {s['stage_key']: s for s in result['stages']}
        codes = [rows[k]['display_code'] for k in ordered_stage_keys(result)]
        self.assertLess(codes.index('X-10'), codes.index('X-1'))
        self.assertEqual(normalize_structure(data, {'excluded_stages': [{'stage_id': 'id-X-1'}]})['code'], 'missing_evidence')

    def test_phase_opening_without_members_never_invents_mapping(self):
        link = {'phase_id': 'future', 'opening_status': 'closed',
                'source_refs': [{'source': 'https://example.invalid/opening'}]}
        result = normalize_structure(catalog(), {'phase_links': [link]})
        self.assertEqual(result['unknown_phase_links'], [link])
        self.assertTrue(all(s['phase_id'] is None and s['opening_status'] == 'unknown'
                            for s in result['stages']))
        link['stage_ids'] = ['id-X-1']
        result = normalize_structure(catalog(), {'phase_links': [link], 'navigation_regions': [
            {'navigation_region_id': 'east', 'stage_ids': ['id-X-1'],
             'source_refs': [{'source': 'https://example.invalid/map'}]}]})
        stage = next(s for s in result['stages'] if s['stage_id'] == 'id-X-1')
        self.assertEqual((stage['phase_id'], stage['navigation_region_id'], stage['opening_status']),
                         ('future', 'east', 'closed'))
        self.assertFalse(stage['account_unlock_verified'])

    def test_cycles_duplicate_members_unknown_exclusions_and_bad_mapping_rejected(self):
        refs = [{'source': 'https://example.invalid/facts'}]
        proposals = [
            ({'order_constraints': [{'before': 'id-X-1', 'after': 'id-X-10', 'source_refs': refs},
                                    {'before': 'id-X-10', 'after': 'id-X-1', 'source_refs': refs}]}, 'order_cycle'),
            ({'excluded_stages': [{'stage_id': 'absent', 'reason': 'tutorial', 'source_refs': refs}]}, 'unknown_stage_ids'),
            ({'query_mappings': [{'stage_id': 'absent', 'query_key': 'id-X-1', 'difficulty': 1,
                                 'source_refs': refs}]}, 'unknown_stage_ids'),
            ({'groups': [{'group_id': 'g', 'label': '离线群岛', 'stage_ids': ['id-X-1', 'id-X-1']}]}, 'duplicate_members')]
        for proposal, expected in proposals:
            with self.subTest(expected=expected):
                self.assertEqual(normalize_structure(catalog(), proposal)['code'], expected)

    def test_explicit_exclusion_retained_and_identity_stable_across_order(self):
        data = catalog()
        first = normalize_structure(data, {})
        data['stages'].reverse()
        second = normalize_structure(data, {})
        self.assertEqual(ordered_stage_keys(first), ordered_stage_keys(second))
        result = normalize_structure(data, {'excluded_stages': [{'stage_id': 'id-X-1',
            'reason': 'out_of_user_scope', 'source_refs': [{'source': 'user-request'}]}]})
        self.assertEqual(len(result['stages']), 4)
        self.assertEqual(len(ordered_stage_keys(result)), 3)
        self.assertEqual(len(result['excluded_stage_keys']), 1)


if __name__ == '__main__':
    unittest.main()

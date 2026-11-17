"""Public map membership organizes import batches, never authorizes gameplay."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from shu.activity_public_facts import stage_facts
from shu.activity_structure import normalize_structure, ordered_stage_keys
from shu.maa_agents import MaaRoleAgents


class QueueNavigationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = {
            'one': self.row('one', 'UNIT-1', 'east'),
            'raid': self.row('raid', 'UNIT-1', 'east', 'FOUR_STAR', level='one'),
            'two': self.row('two', 'UNIT-2', 'west'),
            'training': self.row('training', 'UNIT-TR-1', 'east', appearance='TRAINING'),
            'story': self.row('story', 'UNIT-ST-1', 'east', kind='STORY'),
        }
        self.zones = {key: {'zoneID':key, 'zoneNameSecond':'同名章节', 'zoneIndex':index,
                            'type':'ACTIVITY'} for index,key in enumerate(('east','west'))}
        self.valid = {'east':{'startTs':1, 'endTs':99}, 'west':{'startTs':200, 'endTs':300}}
        self.catalog = {'event_name':'测试岛屿', 'activity_id':'fixture-activity',
            'catalog_complete':True, 'total_stages':len(self.rows),
            'stages':[{'stage_id':key, 'code':row['code']} for key,row in self.rows.items()]}
        path = self.root/'catalog.json'
        path.write_text(json.dumps(self.catalog, ensure_ascii=False), encoding='utf-8')
        self.catalog_ref = {'path':str(path), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    @staticmethod
    def row(sid, code, zone, difficulty='NORMAL', *, level=None, kind='ACTIVITY', appearance='MAIN_NORMAL'):
        return {'stageId':sid, 'code':code, 'zoneId':zone, 'difficulty':difficulty,
                'levelId':level or sid, 'stageType':kind, 'appearanceStyle':appearance,
                'unlockCondition':[]}

    def facts(self):
        def transport(url):
            return json.dumps({'stages':self.rows} if 'stage_table' in url else
                {'zones':self.zones, 'zoneValidInfo':self.valid}).encode('utf-8')
        return stage_facts(self.catalog, self.root/'public-facts', transport=transport, now=100)

    def test_real_zone_identity_and_hashed_sources_generate_distinct_regions(self):
        facts = self.facts()
        regions = {region['navigation_region_id']:region for region in facts.get('navigation_regions', [])}
        self.assertEqual(set(regions), {'public-zone:east','public-zone:west'})
        self.assertEqual(set(regions['public-zone:east']['stage_ids']), {'one','raid','training','story'})
        for region in regions.values():
            self.assertEqual(region['basis'], 'public_zone_membership')
            self.assertIs(region['navigation_verified'], False)
            self.assertEqual(len(region['source_refs']), 2)
            for ref in region['source_refs']:
                self.assertEqual(hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest(), ref['sha256'])

    def test_conflicting_or_missing_zone_identity_never_invents_region(self):
        self.zones['east']['zoneID'] = 'another-zone'
        self.rows['two']['zoneId'] = 'not-present'
        facts = self.facts()
        self.assertEqual(facts.get('navigation_regions'), [])
        unknown = {row['reason'] for row in facts['unknown']}
        self.assertIn('public_zone_identity_conflict', unknown)
        self.assertIn('public_zone_missing', unknown)
        self.assertEqual([row['reason'] for row in facts['unknown'] if row['stage_id']=='one'],
                         ['public_zone_identity_conflict'])
        self.assertEqual({row['stage_id'] for row in facts['query_mappings']}, set(self.rows))

    def test_unhashed_public_source_cannot_claim_navigation_membership(self):
        tables = [({'body':{'stages':self.rows}}, {}),
                  ({'body':{'zones':self.zones, 'zoneValidInfo':self.valid}}, {})]
        with patch('shu.activity_public_facts._table', side_effect=tables):
            facts = stage_facts(self.catalog, self.root/'empty-proofs', now=100)
        self.assertEqual(facts.get('navigation_regions'), [])
        self.assertIn('public_zone_source_unverified', {row['reason'] for row in facts['unknown']})

    def test_normalizer_preserves_public_basis_but_never_navigation_success(self):
        facts = self.facts()
        proposal = {key:copy.deepcopy(facts.get(key,[])) for key in
                    ('query_mappings','groups','phase_links','navigation_regions','order_constraints')}
        for region in proposal['navigation_regions']:
            region['navigation_verified'] = True
        result = normalize_structure(self.catalog, proposal)
        self.assertTrue(result['ok'], result)
        self.assertEqual({row['navigation_region_id'] for row in result['stages']},
                         {'public-zone:east','public-zone:west'})
        for row in result['stages']:
            self.assertEqual(row.get('navigation_basis'), 'public_zone_membership')
            self.assertIs(row.get('navigation_verified'), False)
            self.assertFalse(row['account_unlock_verified'])
        self.assertEqual({row['difficulty'] for row in result['stages'] if row['display_code']=='UNIT-1'}, {1,2})

    def test_public_zone_basis_requires_hashed_reference_shape(self):
        facts = self.facts()
        proposal = {key:copy.deepcopy(facts.get(key,[])) for key in
                    ('query_mappings','groups','phase_links','navigation_regions','order_constraints')}
        # A bare website label is not this tool's hashed zone-table evidence.
        proposal['navigation_regions'] = [{'navigation_region_id':'public-zone:fake',
            'basis':'public_zone_membership', 'stage_ids':['one'],
            'source_refs':[{'source':'https://example.invalid/map'}]}]
        result = normalize_structure(self.catalog, proposal)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'missing_evidence')

    def normalize_tool(self, typed, *, proposal=None, stage_code=None):
        facts = self.facts()
        agent = MaaRoleAgents.__new__(MaaRoleAgents)
        agent.directory = self.root
        agent._guard = Mock()
        agent._publish = Mock(return_value={'path':'fixture-result', 'sha256':'d'*64})
        context = {'task_id':'task', 'epoch':1, 'dispatch_id':'catalog-dispatch',
                   'contract':{'current_deliverable':typed, 'goal':'导入活动全部作业',
                               'task_spec':{'stage_code':stage_code}}}
        state = {'activity_catalog':{'ok':True,'stage_catalog_ref':self.catalog_ref}, 'stage_facts':facts}
        with patch('shu.activity_context.read_activity_catalog', return_value=self.catalog):
            result = agent._execute_tool('catalog','normalize_structure',{'proposal':proposal or {}},
                                         context,state,Mock())
        return result, agent._publish

    def test_import_tool_excludes_teaching_story_and_future_without_erasing_catalog(self):
        result,publish = self.normalize_tool('maa_import')
        self.assertTrue(result['ok'], result)
        structure = publish.call_args.args[1]
        self.assertEqual(len(structure['stages']), 5)
        index = {stage['stage_key']:stage for stage in structure['stages']}
        selected = [index[key] for key in ordered_stage_keys(structure)]
        self.assertEqual({stage['stage_id'] for stage in selected}, {'one','raid'})
        self.assertEqual({stage['navigation_region_id'] for stage in selected}, {'public-zone:east'})
        self.assertEqual({stage['opening_status'] for stage in selected}, {'ended'})
        excluded = {item['stage_id']:item for item in structure['exclusions']}
        for stage in structure['stages']:
            if stage['stage_id'] in {'training','story','two'}:
                self.assertIn(stage['stage_id'], excluded)
        self.assertTrue(any(item.get('opens_at') for item in structure['exclusions']))

    def test_stage_catalog_keeps_all_members_but_projects_regions(self):
        result,publish = self.normalize_tool('stage_catalog')
        self.assertTrue(result['ok'], result)
        structure = publish.call_args.args[1]
        self.assertEqual(len(ordered_stage_keys(structure)), 5)
        self.assertTrue(all(stage.get('navigation_verified') is False for stage in structure['stages']))
        self.assertTrue(all(stage.get('navigation_basis')=='public_zone_membership' for stage in structure['stages']))

    def test_model_proposal_cannot_supply_its_own_navigation_claim(self):
        with self.assertRaises(ValueError):
            self.normalize_tool('maa_import', proposal={'navigation_regions':[{
                'navigation_region_id':'guessed','stage_ids':['one'],'navigation_verified':True}]})


if __name__=='__main__':
    unittest.main()

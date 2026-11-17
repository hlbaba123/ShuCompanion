import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from shu.copilot_catalog import parse_candidate
from shu.roster_match import assess_metadata
from shu.copilot_pool import CopilotPool
from shu.providers import ProviderConfig
from shu.research_tools import ResearchTools


class MetadataIntegrityTests(unittest.TestCase):
    def candidate(self, **body):
        return parse_candidate({'id': 12, 'content': json.dumps(dict(
            stage_name='unit_stage', opers=[{'name': '甲'}], difficulty=2, **body))})

    def test_bad_second_operator_never_becomes_satisfied(self):
        row = {'id': 12, 'content': json.dumps({'stage_name': 'unit_stage', 'difficulty': 2,
            'opers': [{'name': '甲'}, {'skill': 2}], 'actions': [{'secret': 'discard'}]})}
        candidate = parse_candidate(row)
        self.assertFalse(candidate['requirements_complete'])
        self.assertEqual(candidate['source_counts']['operators'], 2)
        self.assertEqual(candidate['difficulty'], 2)
        self.assertTrue(candidate['parse_issues'])
        box = {'operators': [{'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2}],
               'complete': True}
        self.assertEqual(assess_metadata(candidate, box)['classification'], 'unknown')
        self.assertNotIn('actions', candidate)

    def test_bad_group_and_requirements_remain_unknown(self):
        for bad in ({'groups': [None]}, {'groups': [{'name': 'x', 'opers': []}]},
                    {'opers': [{'name': '甲', 'requirements': []}]}):
            body = {'stage_name': 'unit_stage', 'difficulty': 1, 'opers': [{'name': '甲'}]}
            body.update(bad)
            candidate = parse_candidate({'id': 12, 'content': json.dumps(body)})
            self.assertFalse(candidate['requirements_complete'])
            self.assertEqual(assess_metadata(candidate, {})['classification'], 'unknown')

    def test_missing_invalid_and_zero_difficulty_are_unknown(self):
        for value in (None, 0, True, '1', 9):
            body = {'stage_name': 'unit_stage', 'opers': [{'name': '甲'}]}
            if value is not None:
                body['difficulty'] = value
            candidate = parse_candidate({'id': 12, 'content': json.dumps(body)})
            result = assess_metadata(candidate, {})
            self.assertEqual(result['classification'], 'unknown')
            self.assertIn('requirements_sha256', result)

    def test_modes_and_full_requirements_retained(self):
        for mode in (0, 1, 2, 3):
            body = {'stage_name': 'unit_stage', 'difficulty': mode,
                    'opers': [{'name': str(i), 'requirements': {'future': i}} for i in range(30)]}
            candidate = parse_candidate({'id': 12, 'content': json.dumps(body)})
            self.assertEqual(candidate['difficulty'], mode)
            self.assertEqual(len(candidate['operators']), 30)
            self.assertEqual(candidate['operators'][-1]['requirements'], {'future': 29})

    def test_controlled_page_reserves_after_cache_and_continues_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            tools = ResearchTools(ProviderConfig(), Path(temp))
            stage = {'query_key': 'unit_stage', 'display_code': 'SN-EX-1',
                     'difficulty': 2, 'stage_id': 'unit_stage_challenge'}
            calls = []
            def page(query, **kwargs):
                calls.append(('network', kwargs['page']))
                rows = []
                for mode in (1, 2, 3, 0):
                    rows.append(parse_candidate({'id': kwargs['page'] * 10 + mode,
                        'content': json.dumps({'stage_name': 'SN-EX-1', 'difficulty': mode,
                                              'opers': [{'name': '甲'}]})}))
                rows[0]['actions'] = ['must not persist']
                return {'candidates': [], 'unverified_candidates': rows,
                        'source': 'https://prts.maa.plus/copilot/query',
                        'total': 8, 'raw_count': 4, 'has_more': kwargs['page'] == 1}
            with patch('shu.research_tools.query_copilots', side_effect=page) as query:
                cached = tools.candidates_for_stage(stage, cache_only=True)
                self.assertEqual(cached['pages_fetched'], 0)
                self.assertEqual(query.call_count, 0)
                result = tools.candidates_for_stage(stage, before_fetch=lambda number:
                    calls.append(('reserve', number)) or True)
                self.assertEqual(result['pages_fetched'], 1)
                self.assertEqual(result['coverage']['next_page'], 2)
                self.assertEqual(calls, [('reserve', 1), ('network', 1)])
                pool = CopilotPool(Path(temp)).read(result['pool_ref'])
                self.assertEqual(len(pool['candidates']), 4)
                self.assertEqual([x['difficulty_match'] for x in pool['candidates']],
                                 ['incompatible', 'compatible', 'compatible', 'unknown'])
                self.assertTrue(all('actions' not in x for x in pool['candidates']))
                tools.candidates_for_stage(stage, cache_only=True)
                self.assertEqual(query.call_count, 1)
                denied = tools.candidates_for_stage(stage, before_fetch=lambda number: False)
                self.assertEqual(query.call_count, 1)
                self.assertEqual(denied['coverage']['next_page'], 2)
                final = tools.candidates_for_stage(stage, before_fetch=lambda number: True)
                self.assertEqual(query.call_count, 2)
                self.assertTrue(final['coverage']['exhausted'])
                self.assertEqual(len(CopilotPool(Path(temp)).read(final['pool_ref'])['candidates']), 8)

    def test_same_display_code_different_target_and_missing_mode_no_default(self):
        with tempfile.TemporaryDirectory() as temp:
            tools = ResearchTools(ProviderConfig(), Path(temp))
            candidate = self.candidate()
            page = {'candidates': [candidate], 'source': 'https://prts.maa.plus/copilot/query',
                    'total': 1, 'raw_count': 1, 'has_more': False}
            with patch('shu.research_tools.query_copilots', return_value=page) as query:
                result = tools.candidates_for_stage({'query_key': 'unit_stage', 'display_code': 'EX-1', 'difficulty': 1})
                result = tools.candidates_for_stage({'query_key': 'unit_stage', 'display_code': 'EX-1', 'difficulty': 2}, cache_only=True)
                self.assertEqual(query.call_count, 1)
                pool = CopilotPool(Path(temp)).read(result['pool_ref'])
                self.assertEqual(pool['candidates'][0]['difficulty_match'], 'compatible')

    def test_real_previous_public_snapshot_cannot_acquire_typed_receipt(self):
        # This recorded public snapshot predates difficulty/integrity retention.
        # Its real summaries stay readable, but cannot become new verified codes.
        root = Path(__file__).resolve().parents[1]
        audit = root / 'artifacts/20261107-leader-redesign/code-only-evidence.json'
        if not audit.exists():
            self.skipTest('Recorded public snapshot unavailable')
        evidence = json.loads(audit.read_text(encoding='utf-8'))
        tested = 0
        for ref in evidence['pools']:
            path = root.parent / ref['path']
            if path.exists():
                pool = json.loads(path.read_bytes())
                for candidate in pool['candidates']:
                    self.assertEqual(assess_metadata(candidate, {})['classification'], 'unknown')
                    tested += 1
        self.assertGreater(tested, 0)


    def test_unique_mode_can_resolve_unspecified_candidate_difficulty(self):
        with tempfile.TemporaryDirectory() as temp:
            tools=ResearchTools(ProviderConfig(),Path(temp))
            stage={'query_key':'unit_stage','display_code':'unit_stage','stage_id':'unit_stage',
                   'difficulty':1,'public_level_id':'map','difficulty_domain':[1],
                   'difficulty_domain_complete':True,'difficulty_domain_source_refs':[{'path':'fixture','sha256':'abc'}]}
            item=parse_candidate({'id':12,'content':json.dumps({'stage_name':'unit_stage','opers':[{'name':'甲'}]})})
            with patch('shu.research_tools.query_copilots',return_value={'candidates':[item],'source':'fixture','total':1,'raw_count':1,'has_more':False}):
                result=tools.candidates_for_stage(stage,before_fetch=lambda page:True)
            candidate=CopilotPool(Path(temp)).read(result['pool_ref'])['candidates'][0]
            self.assertIsNone(candidate['declared_difficulty']); self.assertEqual(candidate['inferred_difficulty'],1)
            self.assertEqual(candidate['difficulty_basis'],'unique_public_stage_mode')
            box={'operators':[{'name':'甲','owned':True,'rarity':6,'elite':2}],'complete':True}
            self.assertEqual(assess_metadata(candidate,box)['classification'],'satisfied')
    def test_ambiguous_domain_or_invalid_declaration_never_inferred(self):
        for domain, declared in (([1,2],None),([1],'invalid')):
            with self.subTest(domain=domain,declared=declared), tempfile.TemporaryDirectory() as temp:
                tools=ResearchTools(ProviderConfig(),Path(temp))
                stage={'query_key':'unit_stage','display_code':'unit_stage','stage_id':'unit_stage',
                    'difficulty':1,'public_level_id':'map','difficulty_domain':domain,
                    'difficulty_domain_complete':True,'difficulty_domain_source_refs':[{'path':'fixture','sha256':'abc'}]}
                item=parse_candidate({'id':12,'content':json.dumps({'stage_name':'unit_stage','difficulty':declared,'opers':[{'name':'甲'}]})})
                with patch('shu.research_tools.query_copilots',return_value={'candidates':[item],'source':'fixture','total':1,'raw_count':1,'has_more':False}):
                    result=tools.candidates_for_stage(stage,before_fetch=lambda page:True)
                candidate=CopilotPool(Path(temp)).read(result['pool_ref'])['candidates'][0]
                self.assertNotIn('inferred_difficulty',candidate)
                self.assertEqual(candidate['difficulty_match'],'unknown')

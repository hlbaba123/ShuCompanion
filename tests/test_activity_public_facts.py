import json
from pathlib import Path
import tempfile
import unittest

from shu.activity_public_facts import stage_facts


class PublicFactsTests(unittest.TestCase):
    def test_training_style_is_excluded_even_when_stage_type_is_activity(self):
        with tempfile.TemporaryDirectory() as temp:
            def transport(url):
                return json.dumps({'stages': {'a': {'stageId':'a','code':'X-TR-1','levelId':'training',
                    'difficulty':'NORMAL','stageType':'ACTIVITY','appearanceStyle':'TRAINING','isPredefined':True}}}
                    if 'stage_table' in url else {'zones':{}}).encode()
            result=stage_facts({'stages':[{'stage_id':'a','code':'X-TR-1'}]},Path(temp),transport=transport)
            self.assertEqual(result['default_excluded_stages'][0]['stage_id'],'a')

    def test_mapping_uses_real_difficulty_and_same_level_not_suffix_guess(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog = {'event_name': 'Example', 'stages': [
                {'stage_id': 'opaque_normal', 'code': 'X-1'},
                {'stage_id': 'opaque_challenge', 'code': 'X-1'},
                {'stage_id': 'missing', 'code': 'X-2'}]}
            stages = {'opaque_normal': {'stageId': 'opaque_normal', 'code': 'X-1',
                'levelId': 'same_map', 'difficulty': 'NORMAL', 'zoneId': 'z', 'unlockCondition': []},
                'opaque_challenge': {'stageId': 'opaque_challenge', 'code': 'X-1',
                'levelId': 'same_map', 'difficulty': 'FOUR_STAR', 'zoneId': 'z',
                'unlockCondition': [{'stageId': 'opaque_normal'}]}}
            calls = []
            def transport(url):
                calls.append(url)
                return json.dumps({'stages': stages} if url.endswith('stage_table.json') else {
                    'zones': {'z': {'zoneID': 'z', 'zoneIndex': 1, 'zoneNameSecond': '真实章节'}},
                    'zoneValidInfo': {'z': {'startTs': 200, 'endTs': 1000}}}).encode()
            result = stage_facts(catalog, Path(temp), transport=transport, now=100)
            self.assertEqual(result['query_mappings'][1]['query_key'], 'opaque_normal')
            self.assertEqual(result['query_mappings'][1]['difficulty'], 2)
            self.assertEqual(result['query_mappings'][0]['difficulty_domain'], [1, 2])
            self.assertTrue(result['query_mappings'][0]['difficulty_domain_complete'])
            self.assertEqual(result['groups'][0]['label'], '真实章节')
            self.assertEqual(result['phase_links'][0]['opening_status'], 'closed')
            self.assertEqual(result['unknown'][0]['stage_id'], 'missing')
            again = stage_facts(catalog, Path(temp), transport=transport, now=100)
            self.assertEqual(len(calls), 2)
            self.assertEqual(again['query_mappings'], result['query_mappings'])

    def test_code_conflict_is_unknown_not_silently_rebound(self):
        with tempfile.TemporaryDirectory() as temp:
            def transport(url):
                return json.dumps({'stages': {'a': {'stageId': 'a', 'code': 'WRONG',
                    'difficulty': 'NORMAL', 'zoneId': 'z'}}} if 'stage_table' in url else {'zones': {}}).encode()
            result = stage_facts({'stages': [{'stage_id': 'a', 'code': 'X-1'}]},
                                 Path(temp), transport=transport)
            self.assertFalse(result['query_mappings'])
            self.assertEqual(result['unknown'][0]['reason'], 'public_code_conflict')

    def test_unknown_mode_variant_blocks_unique_mode_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            def transport(url):
                return json.dumps({'stages': {
                    'a': {'stageId':'a','code':'X-1','levelId':'m','difficulty':'NORMAL'},
                    'hidden': {'stageId':'hidden','code':'X-1','levelId':'m','difficulty':'FUTURE_MODE'}}}
                    if 'stage_table' in url else {'zones': {}}).encode()
            result=stage_facts({'stages':[{'stage_id':'a','code':'X-1'}]},Path(temp),transport=transport)
            self.assertFalse(result['query_mappings'][0]['difficulty_domain_complete'])
            self.assertEqual(result['query_mappings'][0]['difficulty_domain'], [])


if __name__ == '__main__': unittest.main()

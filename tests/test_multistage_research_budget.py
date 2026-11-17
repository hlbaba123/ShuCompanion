import tempfile
from pathlib import Path
import unittest
from shu.providers import ProviderConfig
from shu.research_tools import ResearchTools,ResearchToolError


class MultiStageBudgetTests(unittest.TestCase):
    def test_enumerated_stages_do_not_exhaust_budget_after_two_distinct_lookups(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def lookup(q,cache):
                calls.append(q)
                return {'levels':[{'code':q,'stage_id':'unit_'+q,'level_id':'level_'+q,'match':'exact_code_or_id'}]}
            tool=ResearchTools(ProviderConfig(),Path(directory),level_query=lookup)
            codes=[f'UNIT-{i}' for i in range(1,11)];tool.set_level_scope(codes)
            for code in codes:self.assertTrue(tool.levels(code)['ok'])
            for code in codes:self.assertTrue(tool.levels(code)['cached'])
            self.assertEqual(calls,codes)

    def test_failures_still_have_a_two_attempt_limit_per_registered_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            def failure(q,cache):raise ValueError('failed fixture')
            tool=ResearchTools(ProviderConfig(),Path(directory),level_query=failure)
            tool.set_level_scope(['UNIT-1','UNIT-2'])
            for _ in range(3):
                with self.assertRaises(ResearchToolError):tool.levels('UNIT-1')
            self.assertEqual(tool.stats['levels'],2)
            with self.assertRaises(ResearchToolError):tool.levels('UNLISTED-3')
            self.assertEqual(tool.stats['levels'],2)


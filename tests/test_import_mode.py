import unittest
from shu.import_mode import single_document_plan


class ImportModeTests(unittest.TestCase):
    def plan(self):
        return {'targets':[{'mystery_code':'prts://123', 'stage_id':'event_ex1',
            'stage_code':'ZZ-EX-1', 'difficulty':mode, 'source_difficulty':3,
            'document_ref':{'path':'/same.json','sha256':'a'*64}} for mode in (1,2)]}

    def test_one_combined_code_is_single_mode(self):
        self.assertTrue(single_document_plan(self.plan()))

    def test_distinct_documents_stages_or_unsupported_difficulty_are_not_collapsed(self):
        for changes in ({'mystery_code':'prts://456'}, {'stage_code':'ZZ-EX-2'},
                        {'source_difficulty':1}, {'document_ref':{'sha256':'b'*64}}):
            plan=self.plan();plan['targets'][1].update(changes)
            self.assertFalse(single_document_plan(plan))

    def test_empty_duplicate_or_unknown_targets_never_collapse(self):
        self.assertFalse(single_document_plan({}))
        plan=self.plan();plan['targets'][1]['difficulty']=1
        self.assertFalse(single_document_plan(plan))


if __name__=='__main__': unittest.main()

"""Aggregate coverage is checked against a complete ordered plan."""
import copy
import hashlib
import json
import unittest

from shu.leader_policy import verify_queue_completion


class QueueCompletionTests(unittest.TestCase):
    def setUp(self):
        self.contract = {'current_deliverable':'maa_import','task_spec':{'kind':'import'},
                         'allow_start':False,'attempt_limit':0}
        self.plan = {'schema':'maa_queue_plan/v1', 'targets':[{'target_key':'normal'}, {'target_key':'raid'}]}
        raw = json.dumps(self.plan,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        self.plan['plan_sha256'] = hashlib.sha256(raw).hexdigest()
        self.receipt = {'schema':'maa_queue_receipt/v1','plan_sha256':self.plan['plan_sha256'],
            'items':[{'target_key':'normal','status':'reused','row_identity':'row1','owned':False,
                      'operation_ids':[],'evidence_refs':[{'path':'baseline','sha256':'a'*64}]},
                     {'target_key':'raid','status':'added','row_identity':'row2','owned':True,
                      'operation_ids':['op'],'evidence_refs':[{'path':'final','sha256':'b'*64}]}],
            'preserved_baseline':True,'unexpected_rows':[], 'battle_start_submitted':False}

    def check(self, receipt=None, plan=None, contract=None):
        return verify_queue_completion(contract or self.contract,plan or self.plan,
                                       self.receipt if receipt is None else receipt)

    def test_complete_distinct_modes_cover_entire_goal(self):
        self.assertTrue(self.check()['goal_complete'])

    def test_partial_keeps_missing_mode(self):
        result = self.check({**self.receipt,'items':self.receipt['items'][:1]})
        self.assertFalse(result['goal_complete'])
        self.assertEqual(result['missing_target_keys'],['raid'])

    def test_legacy_duplicate_extra_wrong_hash_and_unknown_cannot_complete(self):
        for changes in ({'schema':None},{'preserved_baseline':False},{'unexpected_rows':['other']},
                        {'items':self.receipt['items']+[self.receipt['items'][0]]},
                        {'items':self.receipt['items']+[{'target_key':'other'}]},
                        {'plan_sha256':'0'*64}, {'battle_start_submitted':True}):
            with self.subTest(changes=changes):
                self.assertFalse(self.check({**self.receipt,**changes})['goal_complete'])
        receipt = copy.deepcopy(self.receipt)
        receipt['items'][1]['status'] = 'unknown'
        self.assertFalse(self.check(receipt)['goal_complete'])

    def test_tampered_plan_empty_plan_and_battle_goal_cannot_complete(self):
        self.assertFalse(self.check(plan={**self.plan,'targets':[]})['goal_complete'])
        self.assertFalse(self.check(plan={**self.plan,'targets':list(reversed(self.plan['targets']))})['goal_complete'])
        self.assertFalse(self.check(contract={**self.contract,'goal':'导入后开始作战'})['goal_complete'])


if __name__=='__main__':
    unittest.main()

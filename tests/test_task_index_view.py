import copy
import json
import unittest
from shu.controller import _known_task_view


class TaskIndexViewTests(unittest.TestCase):
    def test_bounded_history_keeps_selected_identity_without_old_contracts(self):
        tasks=[{'task_id':str(i),'status':'completed','goal':'旧说明'*1000,
                'contract':{'goal':'历史细节'*1000,'current_deliverable':'stage_catalog',
                            'activity_name':'活动'+str(i),'constraints':['历史约束'*1000]}}
               for i in range(200)]
        original=copy.deepcopy(tasks)
        view=_known_task_view(tasks,'199')
        self.assertLessEqual(len(view),13)
        self.assertIn('199',[row['task_id'] for row in view])
        self.assertLess(len(json.dumps(view,ensure_ascii=False)),6000)
        self.assertTrue(all('contract' not in row and 'constraints' not in row for row in view))
        self.assertEqual(tasks,original)

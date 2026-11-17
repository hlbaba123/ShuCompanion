import tempfile
import unittest
from shu.research_delivery import deliverables,render_delivery


class ResearchDeliveryTests(unittest.TestCase):
    def test_unproved_model_claim_cannot_be_rendered_as_completed_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            items=deliverables(root,{'summary':'已拿到TL1~10 JSON并匹配阵容','verified_copilots':[{'mystery_code':'prts://123'}]})
            self.assertEqual(items,[])
            text=render_delivery(items,completed=True)
            self.assertIn('尚未取得',text);self.assertNotIn('已拿到',text)

    def test_unmatched_item_cannot_be_reported_as_matching(self):
        text=render_delivery([{'classification':'unknown'}],completed=True,require_roster=True)
        self.assertIn('查询未完成',text);self.assertNotIn('符合本地',text)


if __name__=='__main__':unittest.main()

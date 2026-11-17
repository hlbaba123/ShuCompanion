import json
import unittest
from shu.maa_agents import _roster_view


class RosterFactViewTests(unittest.TestCase):
    def setUp(self):
        self.box={'profile_id':'local-default','box_revision':1,'batch_id':'box-fixture','content_hash':'f'*64,
            'complete':False,'operators':[{'name':f'Operator{i}','id':f'char_{i}','owned':i%2==0,
                'elite':2,'level':60,'mastery':{'1':3},'field_sources':{'elite':{'evidence_ref':'x'*900}}} for i in range(440)]}

    def test_overview_does_not_dump_large_per_field_provenance(self):
        result=_roster_view(self.box)
        self.assertEqual(result['summary']['owned'],220)
        self.assertEqual(result['summary']['not_owned'],220)
        self.assertEqual(result['operators'],[])
        self.assertLess(len(json.dumps(result)),2500)

    def test_exact_named_lookup_preserves_values_and_unknown_outside_partial_table(self):
        result=_roster_view(self.box,['Operator0','Operator1','NotListed'])
        self.assertEqual([x['owned'] for x in result['operators']],[True,False])
        self.assertEqual(result['not_found'],[{'name_or_id':'NotListed','owned':None}])
        self.assertEqual(result['operators'][0]['mastery'],{'1':3})
        self.assertNotIn('field_sources',result['operators'][0])


if __name__=='__main__':unittest.main()

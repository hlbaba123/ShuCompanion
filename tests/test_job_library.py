import tempfile
import unittest
from pathlib import Path
from shu.job_library import JobLibrary, _safe

class PathTests(unittest.TestCase):
    def test_reject_traversal(self):
        for value in ('..', '../escape', 'a\\b', 'C:escape'):
            with self.subTest(value=value), self.assertRaises(ValueError): _safe(value)

    def test_primary_only_export_is_idempotent_and_has_no_fake_backup(self):
        class Plans:
            def current(self, task):
                return {'activity_id':'event', 'activity_name':'活动', 'revision':1,
                    'stages':[{'stage_key':'abcdef', 'display_code':'A-1'}],
                    'groups':[{'label':'实际区域','stage_keys':['abcdef']}],
                    'steps':[{'step_id':'step', 'stage_key':'abcdef', 'result':{'status':'primary_only','primary_ref':{'id':1}}}]}
        class Delivery:
            plans = Plans()
            def read_code(self, ref):
                return {'task_id':'task','step_id':'step','revision':1,'id':1,'code':'prts://1'}
        with tempfile.TemporaryDirectory() as d:
            library = JobLibrary(d, Delivery())
            first = library.export_step('task','step')
            self.assertEqual(first, library.export_step('task','step'))
            self.assertIsNone(first['backup_path'])
            self.assertEqual(Path(first['primary_path']).read_text(encoding='utf8'), 'prts://1\n')
            self.assertIn('暂无备选', Path(first['result_path']).read_text(encoding='utf8'))
    def test_deferred_plan_is_exported_without_claiming_schedule(self):
        class Plans:
            def current(self, task):
                return {'activity_id':'event','activity_name':'活动','revision':1,
                    'stages':[{'stage_key':'abcdef','display_code':'A-1'}],
                    'groups':[{'label':'分区','stage_keys':['abcdef']}],
                    'steps':[{'step_id':'step','stage_key':'abcdef','result':{'status':'primary_only','primary_ref':{'id':1}}}],
                    'deferred_stages':[{'stage_key':'later','stage_code':'A-2','status':'pending_unscheduled','action':'开放后复查'}]}
        class Delivery:
            plans=Plans()
            def read_code(self, ref): return {'task_id':'task','step_id':'step','revision':1,'id':1,'code':'prts://1'}
        with tempfile.TemporaryDirectory() as d:
            output=JobLibrary(d,Delivery()).export_step('task','step')
            self.assertIn('pending_unscheduled',Path(output['deferred_path']).read_text(encoding='utf8'))

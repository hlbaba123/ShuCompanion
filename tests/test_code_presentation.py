"""Display targets without collapsing ordinary, raid, or branch identities."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from shu.code_delivery import CodeDeliveryStore
from shu.core import TaskEngine
from shu.job_library import JobLibrary
from shu.leader_plan import PlanStore
from shu.workflow_store import WorkflowStore


class TargetPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.workflow = WorkflowStore(self.engine)
        self.task = self.workflow.submit('查活动作业', {'current_deliverable': 'copilot_codes'})['task_id']
        self.plans = PlanStore(self.workflow)
        self.deliveries = CodeDeliveryStore(self.workflow, self.root)
        self.stages = [
            {'stage_key': 'ordinary', 'display_code': 'RS-EX-1', 'stage_id': 'ex01', 'difficulty': 1, 'branch': ''},
            {'stage_key': 'raid', 'display_code': 'RS-EX-1', 'stage_id': 'ex01#f#', 'difficulty': 2, 'branch': ''},
            {'stage_key': 'branch-a', 'display_code': 'X-S-1', 'stage_id': 's01', 'difficulty': 1, 'branch': 'A'},
            {'stage_key': 'branch-b', 'display_code': 'X-S-1', 'stage_id': 's01', 'difficulty': 1, 'branch': 'B'},
            {'stage_key': 'unknown', 'display_code': 'X-1', 'stage_id': 'x01', 'difficulty': 0, 'branch': ''},
        ]
        for stage in self.stages:
            stage['group_id'] = 'g'
        structure = {'ok': True, 'catalog_complete': True, 'activity_id': 'event', 'activity_name': '活动',
                     'stages': self.stages, 'groups': [{'group_id': 'g', 'label': '分区',
                     'stage_keys': [s['stage_key'] for s in self.stages]}], 'excluded_stage_keys': []}
        raw = json.dumps(structure, ensure_ascii=False).encode('utf-8')
        path = self.root / 'structure.json'
        path.write_bytes(raw)
        self.plans.register(self.task, expected_revision=1, expected_epoch=1,
            structure_ref={'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()},
            ordered_steps=[s['stage_key'] for s in self.stages], search_policy={})

    def test_summary_keeps_modes_and_branches_visible_without_dropping_targets(self):
        summary = self.deliveries.summary(self.task)
        self.assertEqual([i.get('stage_label') for i in summary['items']], [
            'RS-EX-1（普通）', 'RS-EX-1（突袭）', 'X-S-1（普通；分支 A）',
            'X-S-1（普通；分支 B）', 'X-1（难度未知）'])
        self.assertEqual([i.get('stage_key') for i in summary['items']],
                         ['ordinary', 'raid', 'branch-a', 'branch-b', 'unknown'])
        self.assertEqual([i.get('difficulty') for i in summary['items']], [1, 2, 1, 1, 0])
        self.assertEqual([i.get('branch') for i in summary['items']], ['', '', 'A', 'B', ''])
        self.assertEqual(summary['missing'], [
            'RS-EX-1（普通）', 'RS-EX-1（突袭）', 'X-S-1（普通；分支 A）',
            'X-S-1（普通；分支 B）', 'X-1（难度未知）'])

    def test_exports_retain_the_target_identity_even_when_no_code_was_found(self):
        dispatch = self.workflow.ensure_dispatch(self.task, 'research', 'fixture', {})
        library = JobLibrary(self.root / 'library', self.deliveries)
        exported = []
        for _ in range(2):
            lease = self.plans.claim_next(self.task, expected_revision=1, expected_epoch=1,
                                         dispatch_id=dispatch['dispatch_id'])
            self.plans.close_step(lease, {'status': 'search_incomplete', 'primary_ref': None, 'backup_ref': None})
            exported.append(library.export_step(self.task, lease['step_id']))
        self.assertIsNotNone(exported[0].get('stage_path'), 'Export must identify which difficulty was searched')
        ordinary = json.loads(Path(exported[0]['stage_path']).read_text(encoding='utf-8'))
        raid = json.loads(Path(exported[1]['stage_path']).read_text(encoding='utf-8'))
        self.assertEqual((ordinary['stage_label'], ordinary['difficulty'], ordinary['stage_key']),
                         ('RS-EX-1（普通）', 1, 'ordinary'))
        self.assertEqual((raid['stage_label'], raid['difficulty'], raid['stage_key']),
                         ('RS-EX-1（突袭）', 2, 'raid'))
        self.assertNotEqual(exported[0]['stage_path'], exported[1]['stage_path'])


class CodeRendererTests(unittest.TestCase):
    def test_second_attempt_explains_unverified_candidates_without_claiming_none_exist(self):
        from shu.code_presentation import render_code_summary
        item = {'stage_code': 'RS-EX-8', 'difficulty': 1, 'attempt': 2,
                'status': 'search_incomplete', 'reason': 'catalog_unverified', 'unresolved_count': 17}
        rendered = render_code_summary({'summary': '0/1', 'items': [item]})
        self.assertIn('已补查，已查至目录末尾，部分记录未核实', rendered)
        self.assertIn('仍有候选条件待核实', rendered)
        self.assertNotIn('预算已用完', rendered)

    def test_same_codes_remain_present_for_each_explicit_target(self):
        from shu.code_presentation import render_code_summary
        items = [dict(stage_code='RS-EX-2', difficulty=mode, branch='', stage_key=key,
                      primary_code='prts://30887', backup_code='prts://44847')
                 for mode, key in ((1, 'ordinary'), (2, 'raid'))]
        rendered = render_code_summary({'summary': '2/2', 'items': items})
        self.assertIn('RS-EX-2（普通）：主选 prts://30887；备选 prts://44847', rendered)
        self.assertIn('RS-EX-2（突袭）：主选 prts://30887；备选 prts://44847', rendered)
        self.assertEqual(rendered.count('prts://30887'), 2)

    def test_legacy_unqualified_targets_are_not_assigned_a_difficulty(self):
        from shu.code_presentation import stage_label
        self.assertEqual(stage_label({'display_code': 'A-1'}), 'A-1')
        self.assertEqual(stage_label({'display_code': 'A-1', 'difficulty': None}), 'A-1（难度未知）')


if __name__ == '__main__':
    unittest.main()

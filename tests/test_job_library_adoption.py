"""Local reuse exercises the same pool/screen/receipt gates as live research."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

from shu.code_delivery import CodeDeliveryStore
from shu.core import ActionRejected, TaskEngine
from shu.copilot_pool import CopilotPool
from shu.job_library import JobLibrary
from shu.leader_plan import PlanStore
from shu.maa_roster_tools import RosterTools
from shu.workflow_store import WorkflowStore


class LocalLibraryTests(unittest.TestCase):
    def setUp(self):
        evidence = Path(__file__).resolve().parents[1] / 'artifacts' / '20261111-leader-context-repair' / 'local-library-tests'
        evidence.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=evidence)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.open_engine()
        self.rosters.facts.publish('local-default', [{'name': '甲', 'owned': True, 'rarity': 6, 'elite': 2}],
            True, {'kind': 'user-roster-spreadsheet', 'observed_at': 1000, 'evidence_ref': 'fixture:box'}, explicit_ownership=True)
        self.source = self.write(self.root / 'private-source' / 'public-stage.json', {'source': 'fixture:public-selector', 'stage': 'unit'})

    def open_engine(self):
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.workflow = WorkflowStore(self.engine)
        self.plans = PlanStore(self.workflow)
        self.codes = CodeDeliveryStore(self.workflow, self.root)
        self.rosters = RosterTools(self.workflow, self.root)
        self.library = JobLibrary(self.root / 'job-library', self.codes)

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        path.write_bytes(raw)
        return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}

    def task(self, count=1, *, activity='event', difficulty=1, chapter='chapter-2', source=None, inferred=False):
        state = self.workflow.submit('查询测试章节的作业', {'current_deliverable': 'copilot_codes'})
        stages = [dict(stage_key=f'key-{i}', activity_id=activity, edition='', display_code=f'UNIT-{i}',
            stage_id=f'unit_{i}', query_key=f'unit_{i}', difficulty=difficulty, branch='', group_id=chapter,
            query_key_verified=True, difficulty_verified=True, source_refs=[source or self.source]) for i in range(1, count + 1)]
        if inferred:
            for stage in stages:
                stage.update(public_level_id=stage['stage_id'],difficulty_domain=[difficulty],
                    difficulty_domain_complete=True,difficulty_domain_source_refs=[source or self.source])
        structure = dict(ok=True, schema_version=1, activity_id=activity, activity_name='测试活动',
            catalog_complete=True, stages=stages,
            groups=[dict(group_id=chapter, label='第二章', stage_keys=[s['stage_key'] for s in stages])], excluded_stage_keys=[])
        reference = self.write(self.root / 'tasks' / state['task_id'] / 'structure.json', structure)
        self.plans.register(state['task_id'], expected_revision=1, expected_epoch=1,
            structure_ref=reference, ordered_steps=[s['stage_key'] for s in stages], search_policy={})
        return state['task_id']

    def claim(self, task):
        state = self.workflow.current(task)
        dispatch = self.workflow.ensure_dispatch(task, 'research', 'local-library-test', {})
        return self.plans.claim_next(task, expected_revision=state['revision'], expected_epoch=state['epoch'], dispatch_id=dispatch['dispatch_id'])

    def publish(self, task, *, backup=False, acquired_at=None, export=True):
        lease = self.claim(task)
        stage = lease['resolved_stage']
        ordinal = int(stage['stage_id'].split('_')[-1])
        candidates = []
        for identifier in (ordinal * 10 + 1, ordinal * 10 + 2)[:2 if backup else 1]:
            candidate = dict(id=identifier, source=f'https://prts.maa.plus/copilot/get/{identifier}',
                mystery_code=f'prts://{identifier}', stage_name=stage['stage_id'], schema_version=2,
                operators=[{'name': '甲'}], groups=[], requirements_complete=True, parse_issues=[],
                difficulty=stage['difficulty'], stage_match_verified=True, difficulty_match='compatible', available=True,
                catalog_source='fixture:catalog')
            candidate['candidate_locator'] = {key: copy.deepcopy(candidate[key]) for key in ('id', 'source', 'stage_name', 'difficulty')}
            candidate['candidate_locator']['resolved_stage'] = copy.deepcopy(stage)
            if stage.get('difficulty_domain_complete'):
                candidate.update(difficulty=0,declared_difficulty=0,inferred_difficulty=stage['difficulty'],
                    difficulty_basis='unique_public_stage_mode',difficulty_proof=copy.deepcopy(stage['difficulty_domain_source_refs']))
                candidate['candidate_locator']['difficulty']=0
            candidates.append(candidate)
        pool = dict(schema=1, pool_id=CopilotPool.identity(stage['stage_id'], None), stage_id=stage['stage_id'], document=None,
            candidates=candidates, sources=['fixture:catalog'], updated_at=acquired_at or time.time(),
            coverage={'total': len(candidates), 'collected': len(candidates), 'complete': True, 'next_page': None})
        pool_ref = CopilotPool(self.root / 'research' / task / 'candidate-cache')._save(pool)
        screen = self.rosters.screen_pool('local-default', pool_ref)
        refs = [self.codes.register_code(lease, candidate['candidate_locator'], screen['screen_ref']) for candidate in candidates]
        self.plans.close_step(lease, dict(status='two_verified' if backup else 'primary_only',
            primary_ref=refs[0], backup_ref=refs[1] if backup else None, backup_reason=None if backup else 'catalog_exhausted'))
        return self.library.export_step(task, lease['step_id']) if export else {'step_id': lease['step_id']}

    def test_unique_public_mode_proof_relocation_is_rescreened(self):
        source=self.task(inferred=True)
        self.publish(source)
        target=self.task(inferred=True)
        _,result=self.adopt(target)
        self.assertIsNotNone(result)
        self.assertEqual(result['pages_fetched'],0)

    def lookup(self, task, **kwargs):
        self.assertTrue(callable(getattr(self.library, 'lookup_plan', None)), 'Published receipts must be searchable after restart')
        return self.library.lookup_plan(task, **kwargs)

    def adopt(self, task):
        self.assertTrue(callable(getattr(self.library, 'adopt_step', None)), 'Local verified jobs must be adopted into a fresh lease')
        lease = self.claim(task)
        result = self.library.adopt_step(lease)
        if result:
            self.plans.close_step(lease, result)
        return lease, result

    def test_export_restart_adopts_real_primary_and_backup_into_current_task(self):
        origin = self.task()
        self.publish(origin, backup=True)
        self.engine.close()
        self.open_engine()
        target = self.task()
        writes_enabled = self.workflow.current(target)['writes_enabled']
        lookup = self.lookup(target)
        self.assertEqual([r['stage_key'] for r in lookup['usable']], ['key-1'])
        lease, result = self.adopt(target)
        self.assertEqual(result['status'], 'two_verified')
        self.assertEqual(self.codes.read_code(result['primary_ref'])['code'], 'prts://11')
        self.assertEqual(self.codes.read_code(result['backup_ref'])['code'], 'prts://12')
        self.assertEqual(self.codes.read_code(result['primary_ref'])['task_id'], target)
        self.assertNotEqual(target, origin)
        self.assertEqual(self.workflow.current(target)['writes_enabled'], writes_enabled)
        self.assertEqual(self.workflow.current(target)['contract']['current_deliverable'], 'copilot_codes')
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM workflow_operations WHERE task_id=?', (target,)).fetchone()[0], 0)
        self.assertEqual(result['pages_fetched'], 0)

    def test_five_cached_three_missing_and_missing_does_not_become_negative_cache(self):
        origin = self.task(5)
        for _ in range(5): self.publish(origin)
        target = self.task(8)
        lookup = self.lookup(target)
        self.assertEqual([r['stage_key'] for r in lookup['usable']], ['key-1', 'key-2', 'key-3', 'key-4', 'key-5'])
        self.assertEqual([r['stage_key'] for r in lookup['missing']], ['key-6', 'key-7', 'key-8'])
        self.assertTrue(all(r['status'] == 'missing' for r in lookup['missing']))
        later = self.task(8)
        for _ in range(8): self.publish(later)
        self.assertEqual(self.lookup(target)['missing'], [])

    def test_source_task_files_and_code_rows_can_be_purged_without_breaking_public_proof(self):
        origin = self.task()
        self.publish(origin)
        self.engine._db.execute('DELETE FROM code_receipts WHERE task_id=?', (origin,))
        shutil.rmtree(self.root / 'research' / origin)
        shutil.rmtree(self.root / 'private-source')
        self.source = self.write(self.root / 'private-current' / 'public-stage.json', {'source': 'fixture:public-selector', 'stage': 'unit'})
        target = self.task()
        self.assertEqual(len(self.lookup(target)['usable']), 1)
        _, result = self.adopt(target)
        self.assertEqual(self.codes.read_code(result['primary_ref'])['code'], 'prts://11')

    def test_changed_box_is_reported_and_does_not_issue_current_task_code(self):
        origin = self.task()
        self.publish(origin)
        self.rosters.facts.publish('local-default', [{'name': '甲', 'owned': True, 'rarity': 6, 'elite': 1}],
            True, {'kind': 'user-roster-spreadsheet', 'observed_at': 1100, 'evidence_ref': 'fixture:new-box'}, explicit_ownership=True)
        target = self.task()
        self.assertEqual(self.lookup(target)['missing'][0]['status'], 'box_mismatch')
        _, result = self.adopt(target)
        self.assertIsNone(result)
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM code_receipts WHERE task_id=?', (target,)).fetchone()[0], 0)

    def test_corrupt_published_proof_is_stale_and_is_not_adopted(self):
        origin = self.task()
        self.publish(origin)
        target = self.task()
        ref = self.lookup(target)['usable'][0]['candidate_ref']
        manifest = json.loads(Path(ref['path']).read_bytes())
        Path(manifest['records']['primary']['payload']['screen_ref']['path']).write_text('{}', encoding='utf-8')
        self.assertEqual(self.lookup(target)['missing'][0]['status'], 'stale')
        self.assertIsNone(self.adopt(target)[1])

    def test_changed_catalog_source_is_stale_and_another_difficulty_or_chapter_is_missing(self):
        origin = self.task()
        self.publish(origin)
        changed = self.write(self.root / 'new-source.json', {'source': 'fixture:public-selector', 'stage': 'changed'})
        target = self.task(source=changed)
        self.assertEqual(self.lookup(target)['missing'][0]['status'], 'stale')
        for kwargs in ({'difficulty': 2}, {'chapter': 'chapter-3'}, {'activity': 'other-event'}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.lookup(self.task(**kwargs))['missing'][0]['status'], 'missing')

    def test_old_evidence_is_stale_and_a_revoked_lease_cannot_adopt(self):
        origin = self.task()
        self.publish(origin, acquired_at=time.time() - 100)
        target = self.task()
        self.assertEqual(self.lookup(target, max_age_seconds=60)['missing'][0]['status'], 'stale')
        lease = self.claim(target)
        self.workflow.control(target, 'pause')
        self.assertTrue(callable(getattr(self.library, 'adopt_step', None)))
        with self.assertRaises(ActionRejected): self.library.adopt_step(lease)

    def test_adoption_replay_before_close_reuses_current_receipts_and_one_event(self):
        origin = self.task()
        self.publish(origin, backup=True)
        target = self.task()
        lease = self.claim(target)
        self.assertTrue(callable(getattr(self.library, 'adopt_step', None)))
        first = self.library.adopt_step(lease)
        second = self.library.adopt_step(lease)
        self.assertEqual(first, second)
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM code_receipts WHERE task_id=?', (target,)).fetchone()[0], 2)
        self.assertEqual(self.engine._db.execute("SELECT COUNT(*) FROM workflow_outbox WHERE task_id=? AND type='job_library.adopted'", (target,)).fetchone()[0], 1)

    def test_corrupt_current_source_bytes_are_stale_before_adoption(self):
        origin = self.task()
        self.publish(origin)
        target = self.task()
        Path(self.source['path']).write_text('{"stage":"unverified"}', encoding='utf-8')
        self.assertEqual(self.lookup(target)['missing'][0]['status'], 'stale')
        self.assertIsNone(self.adopt(target)[1])

    def test_corrupt_index_is_a_recoverable_miss_and_preserves_original_file(self):
        origin = self.task()
        self.publish(origin)
        target = self.task()
        index = self.library.directory / 'index.sqlite3'
        index.write_bytes(b'corrupt fixture index')
        self.assertEqual(self.lookup(target)['missing'][0]['status'], 'stale')
        self.assertIsNone(self.adopt(target)[1])
        self.assertEqual(index.read_bytes(), b'corrupt fixture index')

    def test_existing_verified_receipts_are_indexed_without_changing_original_evidence(self):
        origin = self.task(3)
        for _ in range(3): self.publish(origin, export=False)
        before = [tuple(row) for row in self.engine._db.execute('SELECT * FROM code_receipts WHERE task_id=? ORDER BY id', (origin,))]
        target = self.task(3)
        self.assertEqual(len(self.lookup(target)['missing']), 3)
        self.assertTrue(callable(getattr(self.library, 'index_verified_history', None)))
        result = self.library.index_verified_history(limit=2, activity_id='event', stage_keys=['key-1', 'key-3'], max_milliseconds=10000)
        self.assertEqual(result['indexed'], 2)
        self.assertEqual([item['stage_key'] for item in self.lookup(target)['missing']], ['key-2'])
        again = self.library.index_verified_history(limit=2, activity_id='event', stage_keys=['key-1', 'key-3'], max_milliseconds=10000)
        self.assertEqual(again['indexed'], 0)
        self.assertEqual(again['already_indexed'], 2)
        self.assertEqual([tuple(row) for row in self.engine._db.execute('SELECT * FROM code_receipts WHERE task_id=? ORDER BY id', (origin,))], before)

    def test_history_indexing_never_promotes_old_box(self):
        origin = self.task()
        self.publish(origin, export=False)
        self.rosters.facts.publish('local-default', [{'name': '甲', 'owned': True, 'rarity': 6, 'elite': 1}],
            True, {'kind': 'user-roster-spreadsheet', 'observed_at': 1100, 'evidence_ref': 'fixture:new-box'}, explicit_ownership=True)
        self.assertTrue(callable(getattr(self.library, 'index_verified_history', None)))
        result = self.library.index_verified_history(limit=1, max_milliseconds=10000)
        self.assertEqual(result['indexed'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(self.lookup(self.task())['missing'][0]['status'], 'missing')

    def test_history_indexing_rejects_another_task_receipt_in_corrupt_step_result(self):
        origin = self.task()
        self.publish(origin, export=False)
        old_step = self.plans.current(origin)['steps'][0]
        altered = self.task()
        self.publish(altered, export=False)
        self.engine._db.execute('UPDATE leader_steps SET result=? WHERE task_id=?', (json.dumps(old_step['result']), altered))
        self.assertTrue(callable(getattr(self.library, 'index_verified_history', None)))
        result = self.library.index_verified_history(limit=1, max_milliseconds=10000)
        self.assertEqual(result['indexed'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(len(self.lookup(self.task())['usable']), 0)


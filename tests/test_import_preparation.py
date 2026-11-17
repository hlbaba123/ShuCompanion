"""Real SQLite/source receipts and offline public-download boundary checks."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from shu.code_delivery import CodeDeliveryStore
from shu.copilot_catalog import parse_candidate
from shu.copilot_pool import CopilotPool
from shu.core import ActionRejected, TaskEngine
from shu.import_preparation import ImportPreparation
from shu.leader_plan import PlanStore, read_reference
from shu.maa_roster_tools import RosterTools
from shu.maa_agents import MaaRoleAgents
from shu.providers import ProviderConfig
from shu.research_tools import ResearchToolError, ResearchTools
from shu.workflow_store import WorkflowStore
from tests.test_maa_agents import FakeSupervisor, element, snapshot


class ImportPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = TaskEngine(self.root)
        self.addCleanup(self.engine.close)
        self.workflow = WorkflowStore(self.engine)
        self.rosters = RosterTools(self.workflow, self.root)
        self.publish_box()
        source = self.workflow.submit('查询活动作业', {'current_deliverable': 'copilot_codes'})
        self.source = source['task_id']
        self.stage = {'stage_key': 'stage-ordinary', 'display_code': 'A-1',
                      'stage_id': 'unit_stage', 'query_key': 'unit_stage',
                      'query_key_verified': True, 'difficulty_verified': True,
                      'difficulty': 1, 'group_id': 'g'}
        # Real public stage tables exceed the legacy 2 MB download-reference
        # cap. The execution identity must reference their pinned binding.
        table = self.root / 'public-stage-table.json'
        table.write_text(json.dumps({'public': 'x' * (2 * 1024 * 1024)}), encoding='utf-8')
        self.stage['source_refs'] = [{'path': str(table), 'sha256': hashlib.sha256(table.read_bytes()).hexdigest()}]
        for field in ('opening_source_refs', 'difficulty_domain_source_refs'):
            path = self.root / (field + '.json')
            raw = json.dumps({'public_evidence': field}).encode()
            path.write_bytes(raw)
            self.stage[field] = [{'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}]
        structure = {'ok': True, 'catalog_complete': True, 'activity_name': '活动',
                     'activity_id': 'event', 'stages': [self.stage],
                     'groups': [{'group_id': 'g', 'label': '区域', 'stage_keys': ['stage-ordinary']}],
                     'excluded_stage_keys': []}
        path = self.root / 'structure.json'
        raw = json.dumps(structure, ensure_ascii=False).encode()
        path.write_bytes(raw)
        self.plans = PlanStore(self.workflow)
        self.plans.register(self.source, expected_revision=1, expected_epoch=1,
                            structure_ref={'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()},
                            ordered_steps=['stage-ordinary'], search_policy={})
        dispatch = self.workflow.ensure_dispatch(self.source, 'research', 'source', {})
        lease = self.plans.claim_next(self.source, expected_revision=1, expected_epoch=1,
                                     dispatch_id=dispatch['dispatch_id'])
        self.step_id = lease['step_id']
        self.body = {'stage_name': 'unit_stage', 'difficulty': 1,
                     'opers': [{'name': '甲'}], 'actions': [{'type': 'Deploy'}],
                     'doc': {'title': '真实下载作业'}}
        candidate = parse_candidate({'id': 42, 'available': True, 'content': json.dumps(self.body)})
        candidate.update(stage_match_verified=True, difficulty_match='compatible')
        candidate['candidate_locator']['resolved_stage'] = self.stage
        pool = CopilotPool(self.root / 'cache')
        self.pool_ref = pool._save({'schema': 1, 'pool_id': pool.identity('unit_stage', None),
            'stage_id': 'unit_stage', 'document': None, 'candidates': [candidate],
            'unverified_candidates': [], 'sources': ['https://prts.maa.plus/copilot/query'],
            'coverage': {'collected': 1, 'total': 1, 'complete': True, 'exhausted': True,
                         'next_page': None, 'pages_received': 1, 'raw_fetched': 1,
                         'unverified': 0, 'total_changed': False}})
        codes = CodeDeliveryStore(self.workflow, self.root)
        receipt = codes.register_code(lease, candidate['candidate_locator'],
                                     self.rosters.screen_pool('local-default', self.pool_ref)['screen_ref'])
        self.plans.close_step(lease, {'status': 'primary_only', 'primary_ref': receipt, 'backup_ref': None})
        self.context = self.new_context()
        self.bridge = ImportPreparation(self.workflow, self.root)
        self.requests = []

    def publish_box(self, *, owns=True, observed_at=1000):
        return self.rosters.facts.publish('local-default',
            [{'name': '甲', 'owned': owns, 'rarity': 6, 'elite': 2}], True,
            {'kind': 'user-roster-spreadsheet', 'observed_at': observed_at,
             'evidence_ref': 'fixture:roster:' + str(owns)}, explicit_ownership=True)

    def new_context(self, **changes):
        contract = {'goal': '导入 A-1 作业', 'current_deliverable': 'maa_import',
                    'relation': 'followup', 'source_task_id': self.source,
                    'target_stage_codes': ['A-1'], 'profile_id': 'local-default'}
        contract.update(changes)
        state = self.workflow.submit(contract['goal'], contract)
        dispatch = self.workflow.ensure_dispatch(state['task_id'], 'research', 'prepare', {'contract': contract})
        self.workflow.claim_dispatch(dispatch['dispatch_id'], 'fixture-owner')
        return {**{key: state[key] for key in ('task_id', 'revision', 'epoch', 'contract')},
                'dispatch_id': dispatch['dispatch_id']}

    def research(self, hook=None, body=None):
        def transport(req, **kwargs):
            self.assertFalse(self.engine._db.in_transaction, 'download must not hold a SQLite writer')
            self.requests.append(req.full_url)
            self.assertIsNone(req.get_header('Authorization'))
            if hook:
                hook()
            return json.dumps({'status_code': 200, 'data': {'id': 42, 'content': body or self.body}}).encode()
        ctx = self.context
        return ResearchTools(ProviderConfig(), self.root / 'research' / ctx['task_id'] / ctx['dispatch_id'],
                             transport=transport)

    def prepare(self, **kwargs):
        kwargs.setdefault('research', self.research())
        return self.bridge.prepare(self.context, source_task_id=self.source, step_id=self.step_id, **kwargs)

    def test_preparation_exposes_verified_document_mode_separately(self):
        self.body['doc']['details'] = ''
        result = self.prepare()
        self.assertEqual(result.get('source_difficulty'), self.body['difficulty'])
        self.assertEqual(result['candidate_record']['difficulty'], self.stage['difficulty'])
        from shu.maa_queue_receipt import canonical_copilot_document
        canonical = canonical_copilot_document(self.body)
        digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
            separators=(',',':'),allow_nan=False).encode()).hexdigest()
        self.assertEqual(result.get('source_semantic_sha256'),digest)

    def test_prepare_many_checks_duplicate_selection_before_any_download(self):
        selection = {'source_task_id':self.source, 'step_id':self.step_id, 'slot':'primary'}
        with patch('shu.import_preparation.ResearchTools') as factory:
            with self.assertRaises(ValueError):
                self.bridge.prepare_many(self.context, [selection,dict(selection)])
        factory.assert_not_called()

    def test_prepare_many_cannot_treat_primary_and_backup_as_two_targets(self):
        first = {'source_task_id':self.source, 'step_id':self.step_id, 'slot':'primary'}
        with patch('shu.import_preparation.ResearchTools') as factory:
            with self.assertRaises(ValueError):
                self.bridge.prepare_many(self.context, [first,{**first,'slot':'backup'}])
        factory.assert_not_called()

    def test_prepare_many_uses_real_preparation_and_current_source(self):
        research = self.research()
        with patch('shu.import_preparation.ResearchTools', return_value=research):
            results = self.bridge.prepare_many(self.context, [
                {'source_task_id':self.source, 'step_id':self.step_id, 'slot':'primary'}])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(results[0]['candidate_record']['source_binding']['source_task_id'], self.source)
        self.assertEqual(results[0]['source_difficulty'], 1)

    def test_options_lists_only_current_bound_verified_steps(self):
        result = self.bridge.options(self.context)
        self.assertEqual(result['source_task_id'], self.source)
        self.assertEqual(result['source_revision'], 1)
        step = result['steps'][0]
        self.assertEqual((step['step_id'], step['stage_code'], step['difficulty'], step['primary_code']),
                         (self.step_id, 'A-1', 1, 'prts://42'))
        self.assertTrue(step['verified'])
        self.assertIsNone(step['backup_code'])
        conflict = self.new_context(target_stage_codes=['B-2'])
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.options(conflict)

    def make_two_mode_source(self):
        source = self.workflow.submit('双模式来源', {'current_deliverable': 'copilot_codes'})
        self.source = source['task_id']
        stages = [self.stage, {**self.stage, 'stage_key': 'stage-challenge',
                              'stage_id': 'unit_stage_challenge', 'difficulty': 2}]
        structure = {'ok': True, 'catalog_complete': True, 'activity_name': '活动', 'activity_id': 'event',
                     'stages': stages, 'excluded_stage_keys': [],
                     'groups': [{'group_id': 'g', 'label': '区域', 'stage_keys': [s['stage_key'] for s in stages]}]}
        path = self.root / 'two-mode-structure.json'
        raw = json.dumps(structure, ensure_ascii=False).encode()
        path.write_bytes(raw)
        self.plans.register(self.source, expected_revision=1, expected_epoch=1,
            structure_ref={'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()},
            ordered_steps=[s['stage_key'] for s in stages], search_policy={})
        candidates = []
        for number, stage in enumerate(stages):
            candidate = parse_candidate({'id': 42 + number, 'available': True,
                'content': json.dumps({**self.body, 'difficulty': stage['difficulty']})})
            candidate.update(stage_match_verified=True, difficulty_match='compatible')
            candidate['candidate_locator']['resolved_stage'] = stage
            candidates.append(candidate)
        pool = CopilotPool(self.root / 'two-mode-cache')
        pool_ref = pool._save({'schema': 1, 'pool_id': pool.identity('unit_stage', None),
            'stage_id': 'unit_stage', 'document': None, 'candidates': candidates, 'unverified_candidates': [],
            'sources': ['https://prts.maa.plus/copilot/query'],
            'coverage': {'collected': 2, 'total': 2, 'complete': True, 'exhausted': True,
                         'next_page': None, 'pages_received': 1, 'raw_fetched': 2, 'unverified': 0, 'total_changed': False}})
        screen_ref = self.rosters.screen_pool('local-default', pool_ref)['screen_ref']
        codes = CodeDeliveryStore(self.workflow, self.root)
        dispatch = self.workflow.ensure_dispatch(self.source, 'research', 'two-modes', {})
        for candidate in candidates:
            lease = self.plans.claim_next(self.source, expected_revision=1, expected_epoch=1,
                                         dispatch_id=dispatch['dispatch_id'])
            ref = codes.register_code(lease, candidate['candidate_locator'], screen_ref)
            self.plans.close_step(lease, {'status': 'primary_only', 'primary_ref': ref, 'backup_ref': None})
        context = self.new_context()
        options = self.bridge.options(context)
        return context, options

    def test_options_preserves_same_code_ordinary_and_challenge_identity(self):
        context, options = self.make_two_mode_source()
        self.assertEqual([(step['stage_code'], step['difficulty'], step['stage_key']) for step in options['steps']],
                         [('A-1', 1, 'stage-ordinary'), ('A-1', 2, 'stage-challenge')])
        self.assertEqual([step['primary_code'] for step in options['steps']], ['prts://42', 'prts://43'])

    def test_two_prepared_modes_retain_separate_sources_and_box_submission_fence(self):
        self.context, options = self.make_two_mode_source()
        def transport(req, **kwargs):
            self.assertFalse(self.engine._db.in_transaction)
            number = int(req.full_url.rsplit('/',1)[-1])
            body = {**self.body, 'difficulty':1 if number==42 else 2}
            self.requests.append(req.full_url)
            return json.dumps({'status_code':200,'data':{'id':number,'content':body}}).encode()
        def factory(config, cache, **kwargs):
            return ResearchTools(config, cache, transport=transport, **kwargs)
        with patch('shu.import_preparation.ResearchTools', side_effect=factory):
            results = self.bridge.prepare_many(self.context,[{
                'source_task_id':self.source,'step_id':step['step_id'],'slot':'primary'}
                for step in options['steps']])
        self.assertEqual([item['candidate_record']['difficulty'] for item in results],[1,2])
        self.assertEqual([item['source_difficulty'] for item in results],[1,2])
        self.assertEqual(len({item['candidate_ref']['sha256'] for item in results}),2)
        self.assertEqual(len(self.requests),2)
        maa_context = self.use_context(results[0])
        self.bridge.submission_guard(maa_context, results[0]['candidate_record'])
        self.bridge.submission_guard(maa_context, results[1]['candidate_record'])
        self.publish_box(observed_at=2000)
        with self.assertRaises((ActionRejected,ValueError)):
            self.bridge.submission_guard(maa_context, results[1]['candidate_record'])

    def test_linked_research_publishes_full_queue_plan_from_two_verified_modes(self):
        from types import SimpleNamespace
        from shu.planned_agents import run_import_research

        self.stage.update(opening_status='permanent', navigation_region_id='public-zone:fixture')
        self.body['doc']['details'] = '测试说明'
        self.make_two_mode_source()
        context = self.new_context(user_request='把 A-1 普通和突袭的主选导入 MAA',
            task_spec={'kind':'import'}, allow_start=False, attempt_limit=0)

        def transport(req, **kwargs):
            number = int(req.full_url.rsplit('/',1)[-1])
            body = {**self.body, 'difficulty':1 if number==42 else 2}
            self.requests.append(req.full_url)
            return json.dumps({'status_code':200,'data':{'id':number,'content':body}}).encode()

        agents = SimpleNamespace(store=self.workflow, directory=self.root,
            _guard=lambda current,cancel: self.bridge._guard(current,cancel))
        with patch('shu.import_preparation.ResearchTools', side_effect=
                   lambda config,cache,**kwargs: ResearchTools(config,cache,transport=transport,**kwargs)):
            result = run_import_research(agents,context,threading.Event())
        self.assertEqual(result['status'],'researched')
        plan = read_reference(result['queue_plan_ref'], self.root)
        self.assertEqual([target['difficulty'] for target in plan['targets']],[1,2])
        self.assertEqual([target['mystery_code'] for target in plan['targets']],['prts://42','prts://43'])
        self.assertEqual(len({target['target_key'] for target in plan['targets']}),2)
        self.assertEqual(result['candidate_refs'],[target['candidate_ref'] for target in plan['targets']])
        self.assertEqual(len(self.requests),2)

    def test_own_multi_plan_options_match_exact_registered_targets(self):
        _, source_options = self.make_two_mode_source()
        source_plan = self.plans.current(self.source)
        own = self.new_context(source_task_id=None,
            task_spec={'kind':'import'}, allow_start=False, attempt_limit=0)
        keys = [step['stage_key'] for step in source_options['steps']]
        self.plans.register(own['task_id'], expected_revision=own['revision'],
            expected_epoch=own['epoch'], structure_ref=source_plan['structure_ref'],
            ordered_steps=keys, import_stage_keys=keys, search_policy={})
        options = self.bridge.options(own)
        self.assertEqual([row['stage_key'] for row in options['steps']],keys)
        self.assertEqual(options['source_task_id'],own['task_id'])
        self.assertTrue(all(row['primary_code'] is None for row in options['steps']))

    def test_download_binds_complete_candidate_and_current_goal_evidence(self):
        result = self.prepare()
        record = result['candidate_record']
        self.assertEqual(self.requests, ['https://prts.maa.plus/copilot/get/42'])
        self.assertEqual(read_reference(result['candidate_ref'], self.root), record)
        stage = record['goal_stage']
        self.assertEqual(read_reference(stage['evidence_ref'], self.root),
                         {key: value for key, value in stage.items() if key != 'evidence_ref'})
        self.assertEqual((record['task_id'], record['epoch'], record['research_dispatch_id']),
                         (self.context['task_id'], self.context['epoch'], self.context['dispatch_id']))
        self.assertEqual(stage['goal'], self.context['contract']['goal'])
        self.assertEqual(stage['resolved_stage'], {key: value for key, value in self.stage.items()
            if key not in ('source_refs', 'opening_source_refs', 'difficulty_domain_source_refs')})
        self.assertEqual(result['assessment']['classification'], 'satisfied')
        self.assertTrue(result['assessment']['full_document_verified'])
        self.assertFalse(result['assessment']['can_start_battle'])
        self.assertEqual(result['verified_copilot']['mystery_code'], 'prts://42')

    def test_module_restart_reuses_committed_bytes_without_download(self):
        first = self.prepare()
        reopened = TaskEngine(self.root)
        self.addCleanup(reopened.close)
        self.bridge = ImportPreparation(WorkflowStore(reopened), self.root)
        second = self.prepare(research=self.research(hook=lambda: self.fail('cache downloaded again')))
        self.assertEqual(second['candidate_ref'], first['candidate_ref'])
        self.assertEqual(len(self.requests), 1)

    def test_prepared_receipt_passes_existing_persisted_candidate_admission(self):
        prepared = self.prepare()
        result = {'status': 'researched', 'candidate_refs': [prepared['candidate_ref']]}
        context = self.use_context(prepared)
        roles = MaaRoleAgents.__new__(MaaRoleAgents)
        roles.store, roles.directory = self.workflow, self.root
        accepted = roles._candidates({**context, 'role_results': {'research': result}})
        self.assertEqual(accepted, [prepared['candidate_record']])

    def use_context(self, prepared, role='maa', **dispatch_fields):
        self.workflow.finish_dispatch(self.context['dispatch_id'],
            {'status': 'researched', 'candidate_refs': [prepared['candidate_ref']]})
        dispatch = self.workflow.ensure_dispatch(self.context['task_id'], role, 'use-' + role,
                                                 {'contract': self.context['contract'], **dispatch_fields})
        self.workflow.claim_dispatch(dispatch['dispatch_id'], 'fixture-use-owner')
        return {**self.context, **dispatch_fields, 'dispatch_id': dispatch['dispatch_id']}

    def test_compound_import_dispatch_records_only_import_input(self):
        request = '查找 A-1 普通的作业，导入MAA并开始执行'
        self.context = self.new_context(goal=request, user_request=request, original_user_request=request,
            task_spec={'kind': 'import', 'stage_code': 'A-1', 'repeat_count': 1},
            allow_start=True, attempt_limit=1, workflow_scope='maa_import_then_battle',
            execution_restrictions={'battle_start': True, 'direct_game_input': 'navigation_only',
                                    'medicine_limit': 0, 'premium_limit': 0})
        prepared = self.prepare()
        context = self.use_context(prepared, execution_step='import')
        context['role_results'] = {'research': {'status': 'researched',
                                                'candidate_refs': [prepared['candidate_ref']]}}
        roles = MaaRoleAgents.__new__(MaaRoleAgents)
        roles.store, roles.directory, roles._closed = self.workflow, self.root, False
        roles._supervisor_lock = threading.RLock()
        roles.input_permission = roles.screen_permission = lambda: True
        roles.supervisor = FakeSupervisor({})
        roles.supervisor.backend = 'visual'

        def simulated_readback(importer, expected, options, cancel):
            self.assertEqual(expected['code'], 'prts://42')
            self.assertEqual(expected['difficulty'], 1)
            action = {'action': 'replace_code', 'code': expected['code']}
            submitted = importer.submit(action, {}, cancel)
            self.assertTrue(submitted['submission_entered'])
            return {'status': 'import_verified', 'battle_start_submitted': False}

        with patch.object(roles, '_import_vision_readback', return_value=object()), \
             patch.object(roles, '_import_load_monitor', return_value=None), \
             patch('shu.maa_visual_import.VisualCodeImporter.run', new=simulated_readback):
            handle = roles._handler('maa')
            cancel = threading.Event()
            with self.assertRaises(ActionRejected):
                handle({**context, 'execution_step': 'battle'}, cancel)
            with self.assertRaises(ActionRejected):
                research = {**context['role_results']['research'], 'queue_plan_ref': {'path': 'queue'}}
                handle({**context, 'role_results': {'research': research}}, cancel)
            self.assertEqual(roles.supervisor.commands, [])
            result = handle(context, cancel)
        self.assertEqual(result['status'], 'import_verified', result)
        self.assertIs(result['battle_start_submitted'], False)
        row, = self.engine._db.execute('SELECT action, starts, status FROM workflow_operations').fetchall()
        self.assertEqual(json.loads(row['action']), {'kind': 'maa_import', 'action': 'replace_code',
                                                     'code': 'prts://42'})
        self.assertEqual((row['starts'], row['status']), (0, 'verified'))

    def test_validate_use_accepts_current_maa_after_research_completion_without_download(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        self.assertIsNone(self.bridge.validate_use(context, prepared['candidate_record']))
        self.assertEqual(self.requests, ['https://prts.maa.plus/copilot/get/42'])

    def act_prepared(self, prepared, context):
        roles = MaaRoleAgents.__new__(MaaRoleAgents)
        roles.store, roles.directory, roles._closed = self.workflow, self.root, False
        roles.input_permission = lambda: True
        roles.supervisor = FakeSupervisor({})
        context = {**context, 'role_results': {'research': {
            'status': 'researched', 'candidate_refs': [prepared['candidate_ref']]}}}
        state = {'snapshot': snapshot(1, element('box', '作业码', ['set_value'])),
                 'action_seq': 0, 'evidence_refs': [], 'inputs': []}
        return roles._act(context, state, {'element_id': 'box', 'action': 'set_value', 'value': '42'},
                          threading.Event())

    def test_submission_rejects_box_changed_between_validation_and_sqlite_gate(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        original = self.workflow.enter_submission
        def intervene(operation_id, **kwargs):
            self.publish_box(observed_at=1001)
            return original(operation_id, **kwargs)
        self.workflow.enter_submission = intervene
        with self.assertRaises(ActionRejected):
            self.act_prepared(prepared, context)
        row = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations').fetchone()
        self.assertEqual((row['status'], row['submitted_at']), ('not_issued', None))

    def test_submission_rejects_source_revised_between_validation_and_sqlite_gate(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        original = self.workflow.enter_submission
        def intervene(operation_id, **kwargs):
            self.workflow.submit('来源已修订', {'current_deliverable': 'copilot_codes'}, task_id=self.source)
            return original(operation_id, **kwargs)
        self.workflow.enter_submission = intervene
        with self.assertRaises(ActionRejected):
            self.act_prepared(prepared, context)
        row = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations').fetchone()
        self.assertEqual((row['status'], row['submitted_at']), ('not_issued', None))

    def test_submission_rejects_source_trashed_between_validation_and_sqlite_gate(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        original = self.workflow.enter_submission

        def intervene(operation_id, **kwargs):
            self.workflow.management.trash(self.source)
            return original(operation_id, **kwargs)

        self.workflow.enter_submission = intervene
        with self.assertRaises(ActionRejected):
            self.act_prepared(prepared, context)
        row = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations').fetchone()
        self.assertEqual((row['status'],row['submitted_at']),('not_issued',None))

    def test_submission_rejects_dispatch_finished_between_validation_and_sqlite_gate(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        original = self.workflow.enter_submission
        def intervene(operation_id, **kwargs):
            self.workflow.finish_dispatch(context['dispatch_id'], {'status': 'unknown'})
            return original(operation_id, **kwargs)
        self.workflow.enter_submission = intervene
        with self.assertRaises(ActionRejected):
            self.act_prepared(prepared, context)
        row = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations').fetchone()
        self.assertEqual((row['status'], row['submitted_at']), ('not_issued', None))

    def test_submission_accepts_unchanged_prepared_source(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        self.assertEqual(self.act_prepared(prepared, context)['status'], 'control_dispatched')
        row = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations').fetchone()
        self.assertEqual(row['status'], 'verified')
        self.assertIsNotNone(row['submitted_at'])

    def test_ordinary_submission_without_bridge_guard_remains_supported(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        operation = self.workflow.prepare_operation(context['task_id'], context['dispatch_id'], 'ordinary',
                                                    {'kind': 'maa_import'})
        self.workflow.issue_operation(operation['operation_id'])
        self.assertEqual(self.workflow.enter_submission(operation['operation_id'])['status'], 'submitted')

    def guard_operation(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        guard = self.bridge.submission_guard(context, prepared['candidate_record'])
        operation = self.workflow.prepare_operation(context['task_id'], context['dispatch_id'], 'guarded',
                                                    {'kind': 'maa_import'})
        self.workflow.issue_operation(operation['operation_id'])
        return context, guard, operation['operation_id']

    def test_submission_gate_checks_only_database_facts_after_file_validation(self):
        _, guard, operation_id = self.guard_operation()
        with patch.object(Path, 'open', side_effect=AssertionError('file IO inside SQLite gate')):
            self.assertEqual(self.workflow.enter_submission(operation_id, import_guard=guard)['status'], 'submitted')

    def test_submission_gate_rejects_source_contract_changed_without_revision(self):
        _, guard, operation_id = self.guard_operation()
        raw = json.dumps({'current_deliverable': 'copilot_codes', 'goal': '新来源合同'},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.engine._transaction():
            self.engine._db.execute('UPDATE workflow_contracts SET contract=?,payload_hash=? '
                'WHERE task_id=? AND revision=1', (raw, hashlib.sha256(raw.encode()).hexdigest(), self.source))
        with self.assertRaises(ActionRejected):
            self.workflow.enter_submission(operation_id, import_guard=guard)

    def test_submission_gate_rejects_changed_source_step(self):
        _, guard, operation_id = self.guard_operation()
        result = self.plans.current(self.source)['steps'][0]['result']
        result['backup_reason'] = '提交前步骤更新'
        with self.engine._transaction():
            self.engine._db.execute('UPDATE leader_steps SET result=? WHERE task_id=? AND step_id=?',
                                   (json.dumps(result), self.source, self.step_id))
        with self.assertRaises(ActionRejected):
            self.workflow.enter_submission(operation_id, import_guard=guard)

    def test_submission_gate_rejects_changed_code_receipt(self):
        _, guard, operation_id = self.guard_operation()
        with self.engine._transaction():
            self.engine._db.execute('UPDATE code_receipts SET payload=?,hash=? WHERE id=?',
                ('{}', hashlib.sha256(b'{}').hexdigest(), guard['source_binding']['code_ref']['receipt_id']))
        with self.assertRaises(ActionRejected):
            self.workflow.enter_submission(operation_id, import_guard=guard)

    def test_submission_gate_rejects_changed_preparation_cache(self):
        _, guard, operation_id = self.guard_operation()
        with self.engine._transaction():
            self.engine._db.execute('UPDATE import_preparations SET payload=?,payload_hash=? WHERE cache_key=?',
                ('{}', hashlib.sha256(b'{}').hexdigest(), guard['cache_key']))
        with self.assertRaises(ActionRejected):
            self.workflow.enter_submission(operation_id, import_guard=guard)

    def test_submission_gate_retains_pause_revocation(self):
        context, guard, operation_id = self.guard_operation()
        self.workflow.control(context['task_id'], 'pause')
        with self.assertRaises(ActionRejected):
            self.workflow.enter_submission(operation_id, import_guard=guard)
        row = self.engine._db.execute('SELECT status,submitted_at FROM workflow_operations WHERE operation_id=?',
                                     (operation_id,)).fetchone()
        self.assertEqual((row['status'], row['submitted_at']), ('not_issued', None))

    def test_validate_use_allows_current_leader_readonly_preview(self):
        prepared = self.prepare()
        context = self.use_context(prepared, 'leader')
        self.assertIsNone(self.bridge.validate_use(context, prepared['candidate_record']))

    def test_validate_use_rejects_source_revision_changed_since_preparation(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        self.workflow.submit('来源已修订', {'current_deliverable': 'copilot_codes'}, task_id=self.source)
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, prepared['candidate_record'])

    def test_validate_use_rejects_source_contract_hash_changed_at_same_revision(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        encoded = json.dumps({'current_deliverable': 'copilot_codes', 'goal': '变更来源'},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        with self.engine._transaction():
            self.engine._db.execute('UPDATE workflow_contracts SET contract=?,payload_hash=? '
                'WHERE task_id=? AND revision=1',
                (encoded, hashlib.sha256(encoded.encode()).hexdigest(), self.source))
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, prepared['candidate_record'])

    def test_validate_use_rejects_new_box_revision_even_when_requirements_still_satisfied(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        self.publish_box(observed_at=1001)
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, prepared['candidate_record'])

    def test_validate_use_rejects_changed_source_step_result(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        result = self.plans.current(self.source)['steps'][0]['result']
        result['backup_reason'] = '来源步骤已更新'
        with self.engine._transaction():
            self.engine._db.execute('UPDATE leader_steps SET result=? WHERE task_id=? AND step_id=?',
                (json.dumps(result), self.source, self.step_id))
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, prepared['candidate_record'])

    def test_validate_use_rejects_tampered_full_source_evidence_hash(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        Path(prepared['candidate_record']['source_binding']['evidence_ref']['path']).write_text('{}')
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, prepared['candidate_record'])

    def test_validate_use_rejects_changed_bytes_in_each_projected_public_source_group(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        for field in ('source_refs', 'opening_source_refs', 'difficulty_domain_source_refs'):
            with self.subTest(field=field):
                path = Path(self.stage[field][0]['path'])
                original = path.read_bytes()
                try:
                    path.write_bytes(b'{}')
                    with self.assertRaises((ValueError, ActionRejected)):
                        self.bridge.validate_use(context, prepared['candidate_record'])
                finally:
                    path.write_bytes(original)

    def test_preparation_rejects_missing_public_source_before_download(self):
        Path(self.stage['source_refs'][0]['path']).unlink()
        with self.assertRaises((OSError, ValueError, ActionRejected)):
            self.prepare()
        self.assertEqual(self.requests, [])

    def test_validate_use_rejects_replaced_full_or_compact_source_binding(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        original = prepared['candidate_record']
        record = json.loads(json.dumps(original))
        record.pop('source_binding')
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, record)
        for field, value in (('slot', 'backup'), ('source_revision', 2),
                             ('roster_ref', {}), ('code_ref', {'receipt_id': 'other', 'sha256': 'other'})):
            with self.subTest(field=field):
                record = json.loads(json.dumps(original))
                record['source_binding'][field] = value
                with self.assertRaises((ValueError, ActionRejected)):
                    self.bridge.validate_use(context, record)
        binding = read_reference(original['source_binding']['evidence_ref'], self.root)
        binding['source_contract_sha256'] = '0' * 64
        record = json.loads(json.dumps(original))
        record['source_binding']['evidence_ref'] = self.bridge._publish('replaced-source', binding)
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, record)

    def test_validate_use_rejects_record_or_goal_bound_to_other_task_revision_epoch(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        for location in ('record', 'goal_stage'):
            for field, value in (('task_id', self.source), ('revision', 2), ('epoch', 2)):
                with self.subTest(location=location, field=field):
                    record = json.loads(json.dumps(prepared['candidate_record']))
                    target = record if location == 'record' else record['goal_stage']
                    target[field] = value
                    with self.assertRaises((ValueError, ActionRejected)):
                        self.bridge.validate_use(context, record)

    def test_validate_use_rejects_forged_context_pending_dispatch_and_revoked_task(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        forged = copy.deepcopy(context)
        forged['contract']['goal'] = '伪造目标'
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(forged, prepared['candidate_record'])
        dispatch = self.workflow.ensure_dispatch(context['task_id'], 'maa', 'pending-use', {})
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use({**context, 'dispatch_id': dispatch['dispatch_id']},
                                     prepared['candidate_record'])
        self.workflow.control(context['task_id'], 'pause')
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, prepared['candidate_record'])

    def test_validate_use_rejects_different_code_id_or_full_document(self):
        prepared = self.prepare()
        context = self.use_context(prepared)
        record = json.loads(json.dumps(prepared['candidate_record']))
        record['copilot_id'] = '43'
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, record)
        replaced = self.root / 'research' / 'replaced.json'
        raw = json.dumps({**self.body, 'actions': [{'type': 'Retreat'}]}).encode()
        replaced.write_bytes(raw)
        record = json.loads(json.dumps(prepared['candidate_record']))
        record.update(path=str(replaced), sha256=hashlib.sha256(raw).hexdigest())
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, record)
        Path(prepared['candidate_record']['path']).write_text('{}')
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.validate_use(context, prepared['candidate_record'])

    def test_cache_tampering_is_rejected_without_refetch(self):
        first = self.prepare()
        Path(first['candidate_record']['path']).write_text('{}', encoding='utf8')
        with self.assertRaises((ValueError, ActionRejected)):
            self.prepare()
        self.assertEqual(len(self.requests), 1)

    def test_source_revision_change_during_download_never_commits_candidate(self):
        def revise():
            self.workflow.submit('来源已修订', {'current_deliverable': 'copilot_codes'}, task_id=self.source)
        with self.assertRaises((ValueError, ActionRejected)):
            self.prepare(research=self.research(hook=revise))
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM import_preparations').fetchone()[0], 0)

    def test_committed_cache_rejects_source_revision_change(self):
        self.prepare()
        self.workflow.submit('来源已修订', {'current_deliverable': 'copilot_codes'}, task_id=self.source)
        with self.assertRaises((ValueError, ActionRejected)):
            self.prepare()
        self.assertEqual(len(self.requests), 1)

    def test_changed_box_during_download_never_commits_candidate(self):
        with self.assertRaises((ValueError, ActionRejected)):
            self.prepare(research=self.research(hook=lambda: self.publish_box(owns=False)))
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM import_preparations').fetchone()[0], 0)

    def test_cached_candidate_does_not_ignore_current_box_change(self):
        self.prepare()
        self.publish_box(owns=False)
        with self.assertRaises((ValueError, ActionRejected)):
            self.prepare()

    def test_cancel_before_and_during_download_rejected(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ActionRejected):
            self.prepare(cancel_event=cancel)
        self.assertEqual(self.requests, [])
        cancel.clear()
        with self.assertRaises(ActionRejected):
            self.prepare(cancel_event=cancel, research=self.research(hook=cancel.set))
        self.assertEqual(self.engine._db.execute('SELECT COUNT(*) FROM import_preparations').fetchone()[0], 0)

    def test_new_epoch_reuses_bytes_but_gets_current_dispatch_provenance(self):
        first = self.prepare()
        task_id = self.context['task_id']
        self.workflow.control(task_id, 'pause')
        state = self.workflow.control(task_id, 'resume')
        dispatch = self.workflow.ensure_dispatch(task_id, 'research', 'new-epoch', {'contract': state['contract']})
        self.workflow.claim_dispatch(dispatch['dispatch_id'], 'new-owner')
        self.context = {**{key: state[key] for key in ('task_id', 'revision', 'epoch', 'contract')},
                        'dispatch_id': dispatch['dispatch_id']}
        second = self.prepare()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(second['candidate_record']['sha256'], first['candidate_record']['sha256'])
        self.assertNotEqual(second['candidate_ref'], first['candidate_ref'])
        self.assertEqual(second['candidate_record']['research_dispatch_id'], dispatch['dispatch_id'])
        self.assertEqual(second['goal_stage']['epoch'], state['epoch'])

    def test_stopped_current_dispatch_and_forged_context_rejected(self):
        forged = copy.deepcopy(self.context)
        forged['contract']['goal'] = '伪造目标'
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.prepare(forged, source_task_id=self.source, step_id=self.step_id, research=self.research())
        self.workflow.control(self.context['task_id'], 'pause')
        with self.assertRaises((ValueError, ActionRejected)):
            self.prepare()
        self.assertEqual(self.requests, [])

    def test_pending_or_wrong_role_dispatch_and_readonly_contract_rejected(self):
        for role in ('maa', 'research'):
            dispatch = self.workflow.ensure_dispatch(self.context['task_id'], role, role + '-other', {})
            if role == 'maa':
                self.workflow.claim_dispatch(dispatch['dispatch_id'], 'fixture-owner')
            changed = {**self.context, 'dispatch_id': dispatch['dispatch_id']}
            with self.assertRaises(ActionRejected):
                self.bridge.prepare(changed, source_task_id=self.source, step_id=self.step_id,
                                    research=self.research())
        readonly = self.new_context(current_deliverable='copilot_codes')
        with self.assertRaises(ActionRejected):
            self.bridge.options(readonly)
        self.assertEqual(self.requests, [])

    def test_other_source_missing_backup_and_wrong_full_identity_rejected(self):
        other = self.workflow.submit('无关来源', {'current_deliverable': 'copilot_codes'})
        with self.assertRaises((ValueError, ActionRejected)):
            self.bridge.prepare(self.context, source_task_id=other['task_id'], step_id=self.step_id,
                                research=self.research())
        with self.assertRaises((ValueError, ActionRejected)):
            self.prepare(slot='backup')
        for changes in ({'stage_name': 'other_stage'}, {'difficulty': 2}, {'opers': [{'name': '乙'}]}, {'actions': []}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, ActionRejected, ResearchToolError)):
                self.prepare(research=self.research(body={**self.body, **changes}))


if __name__ == '__main__':
    unittest.main()

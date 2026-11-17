"""Pure queue planning against synthetic, explicit evidence; no GUI or IO."""
import copy
import hashlib
import json
import unittest

from shu.maa_queue_plan import build_queue_plan, make_target_key, select_import_stages


def ref(label):
    return {'path': 'fixture/' + label + '.json',
            'sha256': hashlib.sha256(label.encode()).hexdigest()}


def stage(key, mode=1, group='coast', region='actual-region', code=None):
    return {'stage_key': key, 'stage_id': 'id-' + key, 'query_key': 'id-' + key,
            'display_code': code or key.upper(), 'difficulty': mode,
            'query_key_verified': True, 'difficulty_verified': True,
            'group_id': group, 'navigation_region_id': region, 'phase_id': 'phase-a',
            'opening_status': 'open', 'source_refs': [ref('catalog')],
            'opening_source_refs': [ref('opening')]}


def structure(rows, exclusions=()):
    excluded = {entry['stage_key'] for entry in exclusions}
    groups = {}
    for item in rows:
        if item['stage_key'] not in excluded:
            groups.setdefault(item['group_id'], {'group_id': item['group_id'],
                'label': item['group_id'], 'stage_keys': [], 'source_refs': [ref('groups')]})['stage_keys'].append(item['stage_key'])
    return {'ok': True, 'catalog_complete': True, 'activity_id': 'unit-activity',
            'activity_name': '测试活动', 'edition': '', 'stages': copy.deepcopy(rows),
            'groups': list(groups.values()), 'excluded_stage_keys': list(excluded),
            'exclusions': list(copy.deepcopy(exclusions))}


def context(**contract):
    return {'task_id': 'task-a', 'revision': 2, 'epoch': 3, 'contract': {
        'goal': '导入测试活动的作业', 'user_request': '导入测试活动的作业',
        'current_deliverable': 'maa_import', 'allow_start': False, 'attempt_limit': 0,
        'task_spec': {'kind': 'import', 'stage_code': None}, **contract}}


def preparation(item, slot='primary', *, copilot_id='42', source_difficulty=None):
    box = {'profile_id': 'local-default', 'batch_id': 'box-batch', 'box_revision': 1,
           'content_hash': 'a' * 64, 'source': {'kind': 'fixture'}}
    binding = {'source_task_id': 'task-a', 'source_revision': 2,
        'step_id': 'step-' + item['stage_key'], 'slot': slot,
        'code_ref': {'receipt_id': 'receipt-' + item['stage_key'], 'sha256': 'b' * 64},
        'roster_ref': box, 'evidence_ref': ref('source-' + item['stage_key'] + slot)}
    document = ref('document-' + copilot_id)
    resolved = {key: value for key, value in item.items()
                if key not in {'source_refs', 'opening_source_refs', 'difficulty_domain_source_refs'}}
    goal = {'task_id': 'task-a', 'revision': 2, 'epoch': 3,
            'goal': context()['contract']['goal'], 'query': item['query_key'],
            'stage_id': item['stage_id'], 'code': item['display_code'],
            'difficulty': item['difficulty'], 'resolved_stage': resolved,
            'source_binding': binding, 'evidence_ref': ref('goal-' + item['stage_key'])}
    record = {'task_id': 'task-a', 'revision': 2, 'epoch': 3,
        'research_dispatch_id': 'dispatch-a', 'copilot_id': copilot_id,
        **document, 'stage_name': item['stage_id'], 'title': '合成作业', 'action_count': 1,
        'source_url': 'https://prts.maa.plus/copilot/get/' + copilot_id,
        'goal_stage': goal, 'difficulty': item['difficulty'],
        'source_binding': binding, 'roster_ref': box}
    verified = {key: record[key] for key in ('copilot_id', 'path', 'sha256', 'stage_name', 'title', 'source_url')}
    verified.update(code=item['display_code'], mystery_code='prts://' + copilot_id,
                    difficulty=item['difficulty'], detail_url='https://prts.plus/?op=' + copilot_id)
    return {'candidate_record': record, 'candidate_ref': ref('candidate-' + item['stage_key'] + slot),
        'goal_stage': copy.deepcopy(goal), 'verified_copilot': verified,
        'assessment': {'classification': 'satisfied', 'roster_ref': copy.deepcopy(box),
                       'copilot_ref': copy.deepcopy(document), 'full_document_verified': True},
        'cached': False,
        'source_difficulty': item['difficulty'] if source_difficulty is None else source_difficulty}


class QueuePlanTests(unittest.TestCase):
    def build(self, rows, prepared=None, *, ctx=None, exclusions=()):
        return build_queue_plan(ctx or context(), ref('structure'), structure(rows, exclusions),
                                prepared if prepared is not None else [preparation(s) for s in rows])

    def test_two_modes_keep_distinct_targets_even_with_same_combined_code(self):
        rows = [stage('normal', 1, code='UNIT-1'), stage('raid', 2, code='UNIT-1')]
        plan = self.build(rows, [preparation(s, source_difficulty=3) for s in rows])
        self.assertEqual([s['difficulty'] for s in plan['targets']], [1, 2])
        self.assertEqual(len({s['target_key'] for s in plan['targets']}), 2)
        self.assertEqual({s['mystery_code'] for s in plan['targets']}, {'prts://42'})
        self.assertEqual(len(plan['batches']), 2)
        self.assertTrue(all(s['slot'] == 'primary' for s in plan['targets']))

    def test_research_order_is_preserved_without_fixed_normal_ex_s_template(self):
        rows = [stage('unit-9', group='harbor', region='east'),
                stage('unit-2', group='interior', region='west')]
        plan = self.build(rows)
        self.assertEqual([s['stage_key'] for s in plan['targets']], ['unit-9', 'unit-2'])
        self.assertEqual(len(plan['batches']), 2)
        self.assertEqual([b['label'] for b in plan['batches']], ['harbor · 普通', 'interior · 普通'])
        self.assertTrue(all(b['navigation_scope_ref'] == ref('structure') for b in plan['batches']))

    def test_public_partition_never_claims_live_navigation_was_verified(self):
        plan = self.build([stage('unit-1')])
        self.assertTrue(all(batch.get('navigation_verified') is False for batch in plan['batches']))

    def test_source_derived_queue_id_is_preserved_and_not_an_arbitrary_station_id(self):
        item = stage('unit-1')
        prepared = preparation(item)
        prepared['expected_copilot_id'] = 0
        self.assertEqual(self.build([item], [prepared])['targets'][0]['expected_copilot_id'], 0)
        for invalid in (True, -1, 99, '0'):
            prepared['expected_copilot_id'] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'expected_copilot_id'):
                self.build([item], [prepared])

    def test_full_scope_retains_explicit_exclusions_and_deferred_opening(self):
        rows = [stage('unit-1'), stage('future')]
        rows[1]['opening_status'] = 'closed'
        excluded = {'stage_key': 'future', 'reason': '未开放',
                    'opens_at': '2026-11-15T16:00:00+08:00', 'source_refs': [ref('opening')]}
        plan = self.build(rows, [preparation(rows[0])], exclusions=[excluded])
        self.assertEqual([s['stage_key'] for s in plan['targets']], ['unit-1'])
        self.assertEqual(plan['exclusions'], [excluded])
        self.assertEqual(plan['deferred'][0]['stage_key'], 'future')
        self.assertEqual(plan['deferred'][0]['opens_at'], excluded['opens_at'])

    def test_historical_ended_opening_allows_import_but_not_an_execution_claim(self):
        item = {**stage('unit-1'), 'opening_status': 'ended'}
        plan = self.build([item])
        self.assertEqual(len(plan['targets']), 1)
        self.assertEqual(plan['deferred'][0]['stage_key'], 'unit-1')
        self.assertEqual(plan['deferred'][0]['status'], 'execution_opening_unknown')
        self.assertFalse(plan['deferred'][0]['blocks_import'])

    def test_requested_subset_uses_directory_order_and_rejects_missing_codes(self):
        rows = [stage('unit-1'), stage('unit-2'), stage('unit-3')]
        wanted = context(target_stage_codes=['UNIT-3', 'UNIT-1'])
        selected = select_import_stages(rows, wanted['contract'])
        self.assertEqual([s['stage_key'] for s in selected], ['unit-1', 'unit-3'])
        plan = self.build(rows, [preparation(s) for s in selected], ctx=wanted)
        self.assertEqual(len(plan['targets']), 2)
        with self.assertRaises(ValueError):
            select_import_stages(rows, context(target_stage_codes=['UNIT-5'])['contract'])

    def test_literal_scope_cannot_disagree_with_typed_targets(self):
        with self.assertRaises(ValueError):
            select_import_stages([stage('unit-1'), stage('unit-2')],
                context(user_request='导入 UNIT-1', target_stage_codes=['UNIT-2'])['contract'])

    def test_typed_whole_activity_is_not_narrowed_by_group_or_negative_example(self):
        rows = [stage('unit-1'), stage('unit-2')]
        for request in ('该活动 UNIT-EX 系列作业全部导入MAA',
                        '给我找全部作业导入MAA，不要只找UNIT-1'):
            with self.subTest(request=request):
                selected = select_import_stages(rows,
                    context(user_request=request,target_stage_codes=[])['contract'])
                self.assertEqual(len(selected),2)

    def test_same_code_modes_are_not_discarded_without_explicit_mode(self):
        rows = [stage('a', 1, code='UNIT-1'), stage('b', 2, code='UNIT-1')]
        self.assertEqual(len(select_import_stages(rows, context()['contract'])), 2)
        value = select_import_stages(rows, context(user_request='导入 UNIT-1 普通作业')['contract'])
        self.assertEqual([s['difficulty'] for s in value], [1])

    def test_negated_mode_mentions_never_expand_the_import_scope(self):
        rows = [stage('a', 1, code='UNIT-1'), stage('b', 2, code='UNIT-1')]
        for request in ('不要普通，只导入 UNIT-1 突袭', '不打突袭，只要 UNIT-1 普通',
                        '导入 UNIT-1 普通，不要突袭', '普通和突袭都不要'):
            with self.subTest(request=request), self.assertRaises(ValueError):
                select_import_stages(rows, context(user_request=request)['contract'])

    def test_missing_primary_never_uses_backup_implicitly(self):
        item = stage('unit-1')
        with self.assertRaisesRegex(ValueError, 'primary'):
            self.build([item], [preparation(item, 'backup')])
        plan = self.build([item], [preparation(item, 'primary'), preparation(item, 'backup', copilot_id='43')])
        self.assertEqual([s['mystery_code'] for s in plan['targets']], ['prts://42'])

    def test_explicit_backup_is_only_for_selected_target_scope(self):
        rows = [stage('unit-1'), stage('unit-2')]
        selected = context(target_stage_codes=['UNIT-2'], selected_slot='backup')
        plan = self.build(rows, [preparation(rows[1], 'backup')], ctx=selected)
        self.assertEqual([(s['stage_key'], s['slot']) for s in plan['targets']], [('unit-2', 'backup')])

    def test_unknown_mode_region_or_opening_does_not_acquire_a_batch(self):
        for patch in ({'difficulty': 0}, {'difficulty': 3}, {'navigation_region_id': None},
                      {'source_refs': []}, {'opening_status': 'unknown'},
                      {'opening_status': 'closed'}, {'opening_source_refs': []},
                      {'query_key_verified': False}, {'difficulty_verified': False}):
            with self.subTest(patch=patch):
                item = {**stage('unit-1'), **patch}
                with self.assertRaises(ValueError):
                    self.build([item])

    def test_public_metadata_can_accompany_but_never_replace_hash_evidence(self):
        item = stage('unit-1')
        item['source_refs'].append({'source': 'https://example.invalid/catalog'})
        self.assertEqual(len(self.build([item])['targets']), 1)
        for refs in ([{'source': 'https://example.invalid/catalog'}],
                     [{'path': 'fixture/catalog.json', 'sha256': 'wrong'}]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                self.build([{**item, 'source_refs': refs}])

    def test_excluded_member_requires_a_real_reason_and_pinned_provenance(self):
        rows = [stage('unit-1'), stage('unit-2')]
        data = structure(rows)
        data['excluded_stage_keys'] = ['unit-2']
        data['groups'][0]['stage_keys'].remove('unit-2')
        for exclusion in ([], [{'stage_key': 'unit-2', 'source_refs': [ref('why')]}],
                          [{'stage_key': 'unit-2', 'reason': 'not a battle'}]):
            data['exclusions'] = exclusion
            with self.subTest(exclusions=exclusion), self.assertRaises(ValueError):
                build_queue_plan(context(), ref('structure'), data, [preparation(rows[0])])

    def test_missing_preparation_cannot_shrink_full_goal(self):
        rows = [stage('unit-1'), stage('unit-2')]
        with self.assertRaises(ValueError):
            self.build(rows, [preparation(rows[0])])

    def test_body_difficulty_is_required_and_must_cover_selected_mode(self):
        item = stage('unit-1', 2)
        for value in (None, 0, 1, True, '3'):
            prepared = preparation(item)
            if value is None:
                prepared.pop('source_difficulty')
            else:
                prepared['source_difficulty'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build([item], [prepared])

    def test_cross_task_stale_mode_or_roster_receipts_are_rejected(self):
        item = stage('unit-1')
        for field, value in (('task_id', 'other'), ('revision', 1), ('epoch', 2),
                             ('difficulty', 2), ('copilot_id', '99')):
            prepared = preparation(item)
            prepared['candidate_record'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build([item], [prepared])
        prepared = preparation(item)
        prepared['assessment']['roster_ref']['box_revision'] = 2
        with self.assertRaises(ValueError):
            self.build([item], [prepared])

    def test_source_task_and_source_revision_are_bound_to_import_contract(self):
        item = stage('unit-1')
        for patch in ({'source_task_id': 'unrelated'}, {'source_revision': 1}):
            prepared = preparation(item)
            prepared['candidate_record']['source_binding'].update(patch)
            prepared['goal_stage']['source_binding'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.build([item], [prepared])
        linked = preparation(item)
        update = {'source_task_id': 'previous-task', 'source_revision': 7}
        linked['candidate_record']['source_binding'].update(update)
        linked['goal_stage']['source_binding'].update(update)
        plan = self.build([item], [linked], ctx=context(source_task_id='previous-task', source_revision=7))
        self.assertEqual(len(plan['targets']), 1)
        with self.assertRaises(ValueError):
            self.build([item], [linked], ctx=context(source_task_id='previous-task', source_revision=8))

    def test_batch_roster_and_member_integrity_do_not_depend_on_display_name(self):
        rows = [stage('one'), stage('two')]
        prepared = [preparation(s) for s in rows]
        second = prepared[1]
        for container in (second['candidate_record'], second['candidate_record']['source_binding'],
                          second['goal_stage']['source_binding'], second['assessment']):
            container['roster_ref']['box_revision'] = 2
        with self.assertRaises(ValueError):
            self.build(rows, prepared)
        data = structure(rows)
        data['groups'][0]['stage_keys'].pop()
        with self.assertRaises(ValueError):
            build_queue_plan(context(), ref('structure'), data, [preparation(s) for s in rows])

    def test_invalid_refs_or_code_are_rejected_before_delivery(self):
        item = stage('unit-1')
        for field in ('candidate_ref',):
            prepared = preparation(item)
            prepared[field] = {'path': 'somewhere', 'sha256': 'not-a-hash'}
            with self.assertRaises(ValueError):
                self.build([item], [prepared])
        prepared = preparation(item)
        prepared['verified_copilot']['mystery_code'] = 'prts://999'
        with self.assertRaises(ValueError):
            self.build([item], [prepared])

    def test_semantic_identity_is_only_carried_from_pinned_document_processing(self):
        item = stage('unit-1')
        prepared = preparation(item)
        self.assertNotIn('source_semantic_sha256', self.build([item], [prepared])['targets'][0])
        prepared['source_semantic_sha256'] = 'c' * 64
        self.assertEqual(self.build([item], [prepared])['targets'][0].get('source_semantic_sha256'), 'c' * 64)
        for invalid in ('not-hashed', None, True):
            prepared['source_semantic_sha256'] = invalid
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self.build([item], [prepared])

    def test_source_binding_and_goal_receipt_cannot_be_replaced_independently(self):
        item = stage('unit-1')
        for field in ('goal_stage', 'source_binding'):
            prepared = preparation(item)
            if field == 'goal_stage':
                prepared[field]['resolved_stage']['stage_key'] = 'unrelated'
            else:
                prepared['candidate_record'][field]['slot'] = 'backup'
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build([item], [prepared])

    def test_no_executable_or_lookup_contract_can_authorize_import_plan(self):
        item = stage('unit-1')
        for patch in ({'allow_start': True}, {'attempt_limit': 1},
                      {'current_deliverable': 'copilot_codes'}, {'task_spec': {'kind': 'lookup'}}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                self.build([item], ctx=context(**patch))

    def test_duplicate_target_preparations_and_extra_goal_rows_are_rejected(self):
        item = stage('unit-1')
        for prepared in ([preparation(item), preparation(item)],
                         [preparation(item), preparation(stage('unit-2'))]):
            with self.assertRaises(ValueError):
                self.build([item], prepared)

    def test_payload_is_json_stable_defensive_and_target_identity_ignores_code(self):
        rows = [stage('unit-1')]
        prepared = [preparation(rows[0])]
        original = copy.deepcopy(prepared)
        plan = self.build(rows, prepared)
        self.assertEqual(prepared, original)
        self.assertEqual(json.loads(json.dumps(plan)), plan)
        payload = {k: v for k, v in plan.items() if k != 'plan_sha256'}
        expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.assertEqual(plan['plan_sha256'], expected)
        changed = self.build(rows, [preparation(rows[0], copilot_id='99')])
        self.assertEqual(plan['targets'][0]['target_key'], changed['targets'][0]['target_key'])
        self.assertNotEqual(plan['plan_sha256'], changed['plan_sha256'])
        for mode in (0, 3, True):
            with self.assertRaises(ValueError):
                make_target_key('unit-1', mode)


if __name__ == '__main__':
    unittest.main()

"""Model projections preserve decisions/identities without repeating local audits."""
import copy
import importlib
import json
import unittest


try:
    view = importlib.import_module('shu.model_research_view')
except ModuleNotFoundError as exc:
    if exc.name != 'shu.model_research_view':
        raise
    view = None


def ref(name, digit='a', **extra):
    return {'path': 'D:/独立资料目录/长期保留用户原路径/research/task-123/' + name + '.json',
            'sha256': digit * 64, **extra}


def assessment(identifier='101', classification='satisfied'):
    local = {'profile_id': 'local-default', 'box_revision': 'box-revision-1',
             'batch_id': 'roster-batch-1', 'content_hash': '7' * 64,
             'source': {'kind': 'fixture', 'evidence_ref': 'D:/原始表/roster.xlsx#sha256=' + '8' * 64,
                        'raw_import_cells': ['irrelevant full local audit'] * 500}}
    return {'classification': classification, 'profile_id': 'local-default',
        'box_revision': 'box-revision-1', 'policy_sha256': '9' * 64,
        'policy': {'ignore_level': True, 'ignore_skill': True, 'ignore_module': True,
                   'high_rarity_elite': 2, 'require_verified_support': True},
        'roster_ref': local, 'evidence_ref': ref('assessment-' + identifier, 'b'),
        'copilot_ref': {'copilot_id': identifier, **ref('download-' + identifier),
                        'goal_stage': {'code': 'GH-1', 'raw_historical_prose': 'duplicated' * 1000}},
        'ownership_checks': [{'name': '离线干员' + str(i), 'owned': True,
                               'source': {'raw_cells': ['full provenance'] * 100}} for i in range(20)],
        'hard_elite_checks': [{'name': '离线干员' + str(i), 'actual': 2, 'required': 2,
                              'source': {'raw_cells': ['full provenance'] * 100}} for i in range(20)],
        'assignments': [{'name': '离线干员' + str(i), 'origin': 'owned', 'requirement': {'raw': 'x' * 1000}}
                        for i in range(20)],
        'ignored_requirements': [{'name': '离线干员' + str(i), 'field': 'level', 'required': 90} for i in range(20)],
        'missing_requirements': [] if classification == 'satisfied' else [
            {'name': '未知新干员', 'field': 'elite', 'required': 2, 'actual': None, 'state': 'unknown',
             'reason': '用户表未记录此干员，无法核实精英化，不能当成满足'}],
        'support_status': 'not_required', 'requires_full_document_verification': False,
        'full_document_verified': True, 'can_start_battle': False}


def ten_stage_receipt():
    result = {'status': 'researched', 'summary': '已核验的作业结果：GH-1至GH-10已取得真实JSON及本地匹配证据。',
              'candidate_refs': [], 'research_refs': [], 'evidence_refs': [],
              'verified_copilots': [], 'assessments': {}, 'candidate_pools': []}
    for i in range(1, 11):
        identifier = str(100 + i)
        result['candidate_refs'].append(ref('candidate-' + identifier))
        result['verified_copilots'].append({'copilot_id': identifier, 'code': 'GH-' + str(i),
            'mystery_code': 'prts://' + identifier, **ref('full-json-' + identifier, 'c'),
            'stage_name': 'offline_stage_' + str(i), 'title': '离线作业' + str(i),
            'source_url': 'https://prts.maa.plus/copilot/get/' + identifier,
            'detail_url': 'https://prts.plus/?op=' + identifier})
        result['assessments'][identifier] = assessment(identifier)
        result['candidate_pools'].append({'pool_ref': ref('candidate-pools/pool-' + str(i), 'd', pool_id=str(i) * 64),
            'coverage': {'complete': True, 'collected': 25, 'total': 25, 'next_page': None},
            'stage_resolution': {'source': 'https://prts.maa.plus/arknights/level', 'raw_sha256': 'e' * 64,
                'levels': [{'code': 'GH-' + str(i), 'stage_id': 'offline_stage_' + str(i),
                            'level_id': 'offline_level_' + str(i), 'event': '虚构河岸', 'name': '离线关卡'}]},
            'cached': False})
        result['research_refs'].extend([ref('observation-' + identifier + '-' + str(n)) for n in range(2)])
    result['evidence_refs'] = copy.deepcopy(result['candidate_refs'] + result['research_refs'])
    result['activity_scope'] = {'event_name': '虚构河岸', 'required_stage_codes': ['GH-' + str(i) for i in range(1, 11)],
        'catalog_stage_codes': ['GH-' + str(i) for i in range(1, 11)] + ['GH-TR-1'],
        'excluded_stages': [{'code': 'GH-TR-1', 'reason_kind': 'not_ordinary', 'stage_kind': 'tutorial',
            'reason': '按真实代码排除教学关', 'source_quote': 'GH-TR-1',
            'source_url': 'https://prts.maa.plus/arknights/level', 'source_ref': ref('activity-catalog')}],
        'catalog_ref': ref('activity-catalog'), 'coverage_verified': True, 'reason': '覆盖全部普通关'}
    result['activity_catalog'] = {'ok': True, 'event_name': '虚构河岸', 'stage_catalog_ref': ref('activity-catalog'),
        'has_more': False, 'catalog_complete': True, 'raw_sha256': 'e' * 64,
        'source': 'https://prts.maa.plus/arknights/level',
        'stages': [{'code': 'GH-' + str(i), 'stage_id': 'offline_stage_' + str(i), 'level_id': 'offline_level_' + str(i)}
                   for i in range(1, 11)]}
    return result


class ModelResearchViewTests(unittest.TestCase):
    def test_maa_read_evidence_projects_controls_and_independent_preview(self):
        snapshot = {'snapshot_id': 'fresh', 'snapshot_status': 'ok', 'timestamp': 123,
            'possibly_truncated': False, 'examined': 200,
            'window': {'hwnd': 1, 'pid': 2, 'executable': 'D:/MAA/MAA.exe'},
            'elements': [
                {'id': 'file', 'name': '', 'automation_id': 'PART_TextBox', 'patterns': ['set_value'],
                 'value': 'D:/可信来源/selected.json', 'enabled': True, 'offscreen': False, 'read_only': False},
                {'id': 'stop', 'name': '停止', 'patterns': ['invoke'], 'enabled': False, 'offscreen': False},
                {'id': 'option', 'name': '自动编队', 'patterns': ['toggle'], 'toggle': 1, 'offscreen': False},
                {'id': 'mode', 'name': '主线/故事集/SideStory', 'patterns': ['select'],
                 'selected': True, 'offscreen': False},
                {'id': 'preview', 'name': '离线活动 - A-1 - 独立加载预览', 'patterns': [], 'offscreen': False},
                {'id': 'duplicate', 'name': '离线活动 - A-1 - 独立加载预览', 'patterns': [], 'offscreen': False},
                {'id': 'help', 'name': '小提示:\n' + '固定操作帮助' * 1000, 'patterns': [], 'offscreen': False},
                {'id': 'secret', 'name': 'Password', 'value': 'private-value', 'patterns': ['set_value'],
                 'password': True, 'offscreen': False},
                *[{'id': 'layout-' + str(i), 'name': '', 'patterns': [], 'offscreen': False,
                   'rect': {'width': 10, 'height': 10}} for i in range(192)]]}
        source = {'task_id': 'current', 'epoch': 3, 'snapshot': snapshot}
        before = copy.deepcopy(source)
        projected = view.compact_tool_result('read_evidence', source)
        actual = projected['snapshot']
        self.assertTrue(projected['model_view_only'])
        self.assertTrue(projected['full_local_evidence_retained'])
        self.assertEqual(actual['snapshot_id'], 'fresh')
        self.assertIs(actual['possibly_truncated'], False)
        self.assertEqual(actual['original_element_count'], 200)
        self.assertEqual(actual['controls'][0]['value'], 'D:/可信来源/selected.json')
        self.assertTrue(any(item.get('name') == '停止' and item['enabled'] is False for item in actual['controls']))
        self.assertTrue(any(item.get('toggle') == 1 for item in actual['controls']))
        self.assertTrue(any(item.get('selected') is True for item in actual['controls']))
        self.assertEqual(actual['static_texts'], ['离线活动 - A-1 - 独立加载预览'])
        self.assertEqual(actual['static_text_ids'], [['preview', 'duplicate']])
        self.assertGreater(actual['omitted_element_count'], 0)
        self.assertNotIn('private-value', json.dumps(projected))
        self.assertLess(len(json.dumps(projected, ensure_ascii=False).encode('utf-8')), 12000)
        self.assertEqual(source, before)
        operation = {'operation_id': 'op-1', 'response': {'status': 'completed', 'submission_entered': True,
            'result': {'action': 'set_value', 'ok': True, 'result': 'dispatched', 'after': snapshot}}}
        receipt = view.compact_tool_result('read_evidence', operation)
        self.assertTrue(receipt['response']['submission_entered'])
        self.assertEqual(receipt['response']['result']['result'], 'dispatched')
        self.assertEqual(receipt['response']['result']['after']['snapshot_id'], 'fresh')
        self.assertLess(len(json.dumps(receipt, ensure_ascii=False).encode('utf-8')), 12000)

    def test_other_read_evidence_is_unchanged_and_maa_projection_reports_its_budget(self):
        source = {'source_url': 'https://example.org', 'facts': {'complete': False}, 'text': '保留完整出处' * 1000}
        self.assertEqual(view.compact_tool_result('read_evidence', source), source)
        snapshot = {'snapshot_id': 'fresh', 'window': {'hwnd': 1, 'pid': 2}, 'possibly_truncated': True,
            'elements': [{'id': 'field-' + str(i), 'name': '控件' + str(i), 'value': '值' * 2000,
                          'patterns': ['set_value'], 'offscreen': False} for i in range(80)]}
        projected = view.compact_tool_result('read_evidence', {'snapshot': snapshot})
        self.assertLess(len(json.dumps(projected, ensure_ascii=False).encode('utf-8')), 12000)
        self.assertTrue(projected['snapshot']['possibly_truncated'])
        self.assertTrue(projected['snapshot']['model_view_truncated'])
        self.assertGreater(projected['snapshot']['omitted_element_count'], 0)

    def test_catalog_role_receipt_projects_all_stage_modes_and_keeps_evidence_and_unknowns(self):
        project = getattr(view, 'compact_catalog_receipt', None)
        self.assertTrue(callable(project), 'catalog role context still replays the full raw catalog')
        original = {'status': 'researched', 'summary': '完整目录；账号解锁未知',
            'structure_ref': ref('structure'), 'evidence_refs': [ref('observation')],
            'unknowns': ['开放时间尚未核实'],
            'activity_catalog': {'ok': True, 'catalog_complete': True, 'has_more': False,
                'stage_catalog_ref': ref('catalog'), 'source': 'https://prts.wiki/w/Fixture',
                'stages': [{'code': 'X-EX-1', 'stage_id': 's1', 'level_id': 'l1',
                    'difficulty': difficulty, 'branch': 'main', 'phase_id': 'phase-two',
                    'opening_status': 'unknown', 'source_refs': [ref('catalog')],
                    'raw_metadata': 'public-source audit' * 10000} for difficulty in (1, 2)]}}
        before = copy.deepcopy(original)
        projected = project(original)
        self.assertLess(len(json.dumps(projected)), 5000)
        self.assertEqual(projected['structure_ref'], original['structure_ref'])
        self.assertEqual(projected['evidence_refs'], original['evidence_refs'])
        self.assertEqual(projected['unknowns'], ['开放时间尚未核实'])
        self.assertEqual([row['difficulty'] for row in projected['activity_catalog']['stages']], [1, 2])
        self.assertEqual(projected['activity_catalog']['stages'][1]['opening_status'], 'unknown')
        self.assertEqual(original, before)
        self.assertEqual(project(projected), projected)

    def test_planned_receipt_keeps_every_status_gap_code_reference_and_deferred_stage(self):
        project = getattr(view, 'compact_planned_receipt', None)
        self.assertTrue(callable(project), 'planned result projection is missing')
        original = {'status': 'unknown', 'summary': '计划已封口，仍有1项缺口',
            'plan_revision': 3, 'complete': False, 'scope_complete': False,
            'closed_not_retryable': True, 'structural_gaps': [{'step_id': 's2', 'reason': '精英化未知'}],
            'export_errors': [{'step_id': 's1', 'code': 'PermissionError'}],
            'steps': [{'step_id': 's1', 'stage': 'X-1', 'status': 'primary_only'}],
            'items': [{'stage_code': 'X-1', 'status': 'primary_only', 'primary_code': 'prts://101'}],
            'missing': ['X-2'], 'code_refs': [{'receipt_id': 'b' * 64, 'sha256': 'b' * 64}],
            'deferred_stages': [{'stage_key': 'future|1', 'opening_status': 'closed',
                'opens_at': '2030-01-01T16:00:00+08:00', 'source_refs': [ref('future-announcement')]}]}
        projected = project(original)
        for field, value in original.items():
            self.assertEqual(projected[field], value, field)
        self.assertEqual(project(projected), projected)

    def test_dialogue_projection_keeps_tail_constraints_and_never_slices_opaque_context(self):
        project = getattr(view, 'compact_dialogue_context', None)
        self.assertTrue(callable(project), 'dialogue still slices serialized facts at a character offset')
        original = {'task_contract': {'goal': '只查作业', 'constraints': ['禁止开战'],
                                     'reply': '重复的自然回复' * 4000},
            'registered_job_output': {'status': 'unknown', 'items': [], 'missing': ['X-10'],
                                      'summary': '尚无已核验主选'},
            'user_preferences': [{'text': '保留长期来源偏好'}],
            'battle_start_allowed': False, 'unknowns': ['尾部未知事实不能丢失']}
        before = copy.deepcopy(original)
        projected = project(json.dumps(original, ensure_ascii=False))
        self.assertEqual(projected['task_contract']['constraints'], ['禁止开战'])
        self.assertEqual(projected['unknowns'], ['尾部未知事实不能丢失'])
        self.assertEqual(projected['registered_job_output']['missing'], ['X-10'])
        self.assertEqual(projected['battle_start_allowed'], False)
        self.assertNotIn('reply', projected['task_contract'])
        self.assertEqual(original, before)
        text = '不透明上下文' * 4000 + '尾部禁止开战'
        self.assertEqual(project(text), text)

    def test_catalog_tool_view_keeps_all_members_without_repeated_raw_metadata(self):
        source={'ok':True,'tool':'activity_stages','event_name':'Test','catalog_complete':True,
            'total_stages':2,'has_more':False,'stage_catalog_ref':ref('catalog'),
            'stages':[{'code':'X-'+str(i),'stage_id':'s'+str(i),'level_id':'l'+str(i),
                       'name':'关卡','source_refs':[{'verbose':'重复公开元数据'*1000}]} for i in (1,2)]}
        original=copy.deepcopy(source)
        output=view.compact_tool_result('research_activity_stages',source)
        self.assertLess(len(json.dumps(output,ensure_ascii=False)),2000)
        self.assertEqual(output['stage_catalog_ref'],source['stage_catalog_ref'])
        self.assertEqual([s['code'] for s in output['stages']],['X-1','X-2'])
        self.assertEqual(source,original)

    def setUp(self):
        self.assertIsNotNone(view, 'research model projection has not been implemented')

    def test_ten_stage_receipt_stays_under_twenty_kilobytes_without_losing_outputs(self):
        source = ten_stage_receipt()
        before = copy.deepcopy(source)
        projected = view.compact_research_result(source)
        self.assertGreater(len(json.dumps(source, ensure_ascii=False).encode('utf-8')), 500_000)
        self.assertLess(len(json.dumps(projected, ensure_ascii=False).encode('utf-8')), 20_000)
        self.assertEqual(source, before)
        self.assertEqual(len(projected['verified_copilots']), 10)
        for original, actual in zip(source['verified_copilots'], projected['verified_copilots']):
            for field in ('code', 'copilot_id', 'mystery_code', 'stage_name', 'path', 'sha256', 'source_url'):
                self.assertEqual(actual[field], original[field])
        self.assertEqual(projected['candidate_refs'], source['candidate_refs'])
        self.assertEqual(projected['activity_scope'], source['activity_scope'])
        for identifier, actual in projected['assessments'].items():
            self.assertEqual(actual['classification'], 'satisfied')
            self.assertEqual(actual['box_revision'], 'box-revision-1')
            self.assertEqual(actual['policy_sha256'], '9' * 64)
            self.assertEqual(actual['evidence_ref'], source['assessments'][identifier]['evidence_ref'])
            self.assertNotIn('ownership_checks', actual)
            self.assertNotIn('copilot_ref', actual)
        for original, actual in zip(source['candidate_pools'], projected['candidate_pools']):
            self.assertEqual(actual['pool_ref'], original['pool_ref'])
            self.assertEqual(actual['coverage'], original['coverage'])
            self.assertEqual(actual['stage_resolution']['levels'][0]['code'], original['stage_resolution']['levels'][0]['code'])

    def test_failure_and_unknown_reasons_and_unfinished_stage_gaps_are_retained(self):
        source = ten_stage_receipt()
        source['status'] = 'unknown'
        source['summary'] = 'GH-10：缺失当前精英化证据；不能声称已找到可用作业。'
        source['code'] = 'research_no_progress'
        source['unknowns'] = ['GH-10精英化仍未知']
        source['tool_errors'] = [{'tool': 'research_stage_jobs', 'code': 'ResearchToolError', 'reason': '站内预算已用尽'}]
        source['verified_copilots'].pop()
        source['assessments']['109'] = assessment('109', classification='unknown')
        projected = view.compact_research_result(source)
        self.assertEqual(projected['summary'], source['summary'])
        self.assertEqual(projected['tool_errors'], source['tool_errors'])
        self.assertEqual(projected['unknowns'], source['unknowns'])
        self.assertIn('GH-10', projected['missing_stage_codes'])
        self.assertIn('GH-9', projected['unmatched_stage_codes'])
        self.assertEqual(projected['assessments']['109']['missing_requirements'], source['assessments']['109']['missing_requirements'])

    def test_search_and_fetch_return_exact_copied_source_text_for_scope_quotes(self):
        for name in ('research_search', 'research_fetch', 'web_search', 'web_fetch'):
            source = {'ok': True, 'tool': 'fetch', 'url': 'https://example.org/official',
                'data': {'content': '普通关卡GH-1～GH-10于2030年11月12日16:00开放。' * 1000},
                'untrusted_content': True, 'content_is_instruction': False}
            projected = view.compact_tool_result(name, source)
            self.assertEqual(projected, source)
            projected['data']['content'] = 'changed copy'
            self.assertNotEqual(projected, source)

    def test_stage_jobs_keeps_full_verified_identity_and_small_assessment(self):
        source = ten_stage_receipt()
        result = {'ok': True, 'tool': 'stage_jobs', 'stage_code': 'GH-1', 'coverage': source['candidate_pools'][0]['coverage'],
            'pool_ref': source['candidate_pools'][0]['pool_ref'], 'counts': {'satisfied': 25, 'unknown': 0},
            'verified_copilot': source['verified_copilots'][0], 'assessment': source['assessments']['101'],
            'checked': [{'copilot_id': '101', 'classification': 'satisfied'}]}
        before = copy.deepcopy(result)
        projected = view.compact_tool_result('research_stage_jobs', result)
        self.assertLess(len(json.dumps(projected, ensure_ascii=False).encode('utf-8')), 4000)
        self.assertEqual(projected['verified_copilot'], result['verified_copilot'])
        self.assertEqual(projected['checked'], result['checked'])
        self.assertEqual(projected['assessment']['evidence_ref'], result['assessment']['evidence_ref'])
        self.assertEqual(projected['assessment']['policy'], result['assessment']['policy'])
        self.assertEqual(result, before)

    def test_unknown_stage_job_failure_explanation_is_not_clipped(self):
        source = {'ok': False, 'tool': 'stage_jobs', 'stage_code': 'GH-10',
                  'summary': '精英化未核实，不能忽略。' * 800,
                  'checked': [{'copilot_id': '101', 'error': 'ResearchToolError', 'reason': '远端超时，未取得完整JSON'}],
                  'coverage': {'complete': False, 'collected': 100, 'total': 125, 'next_page': 3}}
        self.assertEqual(view.compact_tool_result('research_stage_jobs', source), source)

    def test_pool_screen_keeps_accepted_ids_refs_and_failed_requirements_without_audits(self):
        source = {'ok': True, 'tool': 'screen_pool', 'classification': 'unknown',
            'profile_id': 'local-default', 'box_revision': 'box-revision-1', 'policy_sha256': '9' * 64,
            'pool_ref': ref('pool', pool_id='f' * 64), 'screen_ref': ref('screen', 'd'),
            'coverage': {'complete': True, 'collected': 2}, 'counts': {'unknown': 1, 'satisfied': 1},
            'accepted_count': 1, 'screened_count': 2, 'missing_requirements': [{'field': 'roster', 'reason': '未采集'}],
            'accepted': [{'id': 101, 'source': 'https://prts.maa.plus/copilot/get/101',
                'mystery_code': 'prts://101', 'stage_name': 'offline_stage_1', 'classification': 'satisfied',
                'operators': [{'name': '离线干员', 'origin': 'owned'}],
                'hard_elite_checks': [{'source': {'raw_cells': ['local-only audit'] * 1000}}]}]}
        projected = view.compact_tool_result('research_screen', source)
        self.assertEqual(projected['accepted'][0]['id'], 101)
        self.assertEqual(projected['screen_ref'], source['screen_ref'])
        self.assertEqual(projected['pool_ref'], source['pool_ref'])
        self.assertEqual(projected['missing_requirements'], source['missing_requirements'])
        self.assertNotIn('hard_elite_checks', projected['accepted'][0])

    def test_projection_never_synthesizes_satisfied_or_download_identity(self):
        result = view.compact_research_result({'status': 'unknown', 'summary': '尚无产物',
            'activity_scope': {'required_stage_codes': ['XYZ-EX-12']},
            'verified_copilots': [], 'assessments': {}})
        self.assertEqual(result['verified_copilots'], [])
        self.assertEqual(result['missing_stage_codes'], ['XYZ-EX-12'])
        self.assertNotIn('mystery_code', json.dumps(result))

    def test_success_label_cannot_erase_an_unknown_reason_present_only_in_summary(self):
        source=ten_stage_receipt()
        source['summary']='GH-10曾有一份候选因精英化未知被跳过，最后采用另一份真实JSON；不能说所有候选都可用。'
        self.assertEqual(view.compact_research_result(source)['summary'],source['summary'])

    def test_projecting_an_existing_model_view_preserves_shared_policy_and_roster_identity(self):
        first=view.compact_research_result(ten_stage_receipt())
        self.assertEqual(view.compact_research_result(first),first)

    def test_program_delivery_drops_only_repeated_artifact_lines_and_keeps_extra_failure_reason(self):
        source=ten_stage_receipt()
        item=source['verified_copilots'][0]
        reason='仍需核实GH-10候选的某个本地字段；此原因不能因成功状态被删。'
        source['summary']='\n'.join(['已取得以下真实资料；整项任务尚未完成：',
            '- GH-1：prts://101（符合本地干员硬性条件）','  JSON：'+item['path'],
            '  来源：'+item['detail_url'],reason])
        projected=view.compact_research_result(source)
        self.assertIn(reason,projected['summary'])
        self.assertNotIn(item['path'],projected['summary'])
        self.assertEqual(projected['verified_copilots'][0]['path'],item['path'])


if __name__ == '__main__':
    unittest.main()

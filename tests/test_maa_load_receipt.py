"""MAA application receipts use fresh local files, never GUI or models."""
import base64
import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shu import maa_load_receipt as receipt


IDENTITY = {'hwnd': 41, 'pid': 42, 'creation_time': 43,
            'executable': r'D:\MAA\MAA-v6.21.4-win-x64\MAA.exe'}
CODE = 'prts://108595'
SOURCE = {'stage_name': 'act55side_02', 'minimum_required': 'v6.0.0',
          'doc': {'title': '时间啊，向前！ - TL-2 - 孑遗的道路 - 初雪单核',
                  'details': '独立说明第一行\n第二行，不能当成日志。'},
          'opers': [{'name': '圣聆初雪', 'skill': 2,
                     'requirements': {'elite': 2, 'level': 90, 'module': 2}},
                    {'name': '斑点', 'skill': 1, 'skill_usage': 0,
                     'requirements': {'elite': 1, 'level': 55, 'module': -1}}],
          'actions': [{'type': 'SpeedUp'}, {'type': 'Deploy', 'name': '斑点',
                       'location': [8, 2], 'direction': 'Down'}, {'type': 'SkillDaemon'}]}


class LoadReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.log, self.cache = self.root / 'gui.log', self.root / '_temp_copilot.json'
        self.log.write_bytes(b'old log\n')
        self.cache.write_text(json.dumps(SOURCE, ensure_ascii=False), encoding='utf-8')
        self.addCleanup(patch.stopall)
        patch.object(receipt, '_LOG_PATH', self.log).start()
        patch.object(receipt, '_CACHE_PATH', self.cache).start()
        self.now = datetime(2026, 11, 9, 20, 0, 0).timestamp()
        self.clock = patch.object(receipt.time, 'time', side_effect=lambda: self.now).start()
        self.baseline = receipt.capture_baseline(IDENTITY, SOURCE, CODE, self.root / 'evidence')
        self.now += 1

    def line(self, logger, message, *, level='INF', when=None):
        stamp = datetime.fromtimestamp(self.now if when is None else when).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        return f'[{stamp}][{level}][{logger}]       <2> {message}\n'

    def success(self, *, code=CODE, title=None, details=None, count=2):
        title = SOURCE['doc']['title'] if title is None else title
        details = SOURCE['doc']['details'] if details is None else details
        return (self.line('HttpResponseLoggingExtension',
                          'HTTP: OK GET https://prts.maa.plus/copilot/get/' + code.split('//')[1]
                          + ' null null 20.0ms')
                + self.line('CopilotViewModel', title)
                + self.line('CopilotViewModel', details)
                + self.line('CopilotViewModel', '-' * 48)
                + self.line('CopilotViewModel', f'共 {count} 名干员'))

    def append(self, text, *, advance_cache=True):
        if advance_cache and 'HTTP: OK GET ' in text:
            self.cache.write_bytes(self.cache.read_bytes())
        with self.log.open('ab') as stream:
            stream.write(text.encode('utf-8'))

    def collect(self, identity=None):
        return receipt.collect_receipt(self.baseline, identity or IDENTITY, self.root / 'evidence')

    def payload(self, result):
        ref = result['receipt_ref']
        raw = Path(ref['path']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), ref['sha256'])
        return json.loads(raw)

    def expected(self):
        return {'code': CODE, 'source_document': SOURCE, 'target_identity': IDENTITY}

    def test_fresh_exact_multiline_sequence_and_document_verify_offline(self):
        self.append(self.success())
        result = self.collect()
        self.assertEqual(result['status'], 'verified', result)
        self.assertEqual(result['title_verified_by'], 'maa_log_cache')
        payload = self.payload(result)
        self.assertEqual(receipt.verify_receipt(payload, self.expected())['status'], 'verified')
        self.assertNotIn('expected_title_visible', result)

    def test_only_documented_default_omissions_are_equivalent(self):
        cache = copy.deepcopy(SOURCE)
        cache['groups'] = []
        cache['opers'][1].pop('skill_usage')
        cache['opers'][1]['requirements'].pop('module')
        cache['actions'][1].pop('type')
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        self.append(self.success())
        self.assertTrue(self.collect()['verified'])

    def zero_condition_case(self):
        source = copy.deepcopy(SOURCE)
        source['actions'][1].update(kills=0, costs=0, cost_changes=0, cooling=0)
        self.baseline = receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')
        cache = copy.deepcopy(source)
        for key in ('kills', 'costs', 'cost_changes'):
            del cache['actions'][1][key]
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        self.append(self.success())
        return source, cache

    def test_mainline_zero_action_conditions_are_omitted_by_maa_dto(self):
        source, _ = self.zero_condition_case()
        fingerprint = self.baseline['source_semantic_sha256']
        result = self.collect()
        self.assertTrue(result['verified'], result)
        self.assertEqual(fingerprint, hashlib.sha256(receipt._bytes(receipt._canonical(source))).hexdigest())
        expected = dict(self.expected(), source_document=source)
        self.assertTrue(receipt.verify_receipt(self.payload(result), expected)['verified'])

    def test_action_conditions_only_equate_exact_integer_zero_with_omission(self):
        _, cache = self.zero_condition_case()
        for field in ('kills', 'costs', 'cost_changes'):
            for changed in (1, False, '0', 0.0):
                with self.subTest(field=field, value=changed, value_type=type(changed).__name__):
                    bad = copy.deepcopy(cache)
                    bad['actions'][1][field] = changed
                    self.cache.write_text(json.dumps(bad, ensure_ascii=False), encoding='utf-8')
                    self.assertEqual(self.collect()['code'], 'source_body_changed')

    def test_zero_condition_normalization_does_not_hide_other_action_changes(self):
        _, cache = self.zero_condition_case()
        for change in ({'cooling': -1}, {'location': [8, 3]}, {'name': '另一干员'},
                       {'direction': 'Up'}, {'type': 'Retreat'}, {'unknown_condition': 0}):
            with self.subTest(change=change):
                bad = copy.deepcopy(cache)
                bad['actions'][1].update(change)
                self.cache.write_text(json.dumps(bad, ensure_ascii=False), encoding='utf-8')
                self.assertEqual(self.collect()['code'], 'source_body_changed')

    def test_robot_zero_elite_default_can_be_omitted_but_not_changed(self):
        source=copy.deepcopy(SOURCE)
        source['opers'][1]={'name':'Lancet-2','requirements':{
            'elite':0,'level':30,'module':-1,'potentiality':1,'skill_level':1}}
        self.baseline=receipt.capture_baseline(IDENTITY,source,CODE,self.root/'evidence')
        cached=copy.deepcopy(source)
        cached['opers'][1]['requirements'].pop('elite')
        cached['opers'][1]['requirements'].pop('module')
        self.cache.write_text(json.dumps(cached,ensure_ascii=False),encoding='utf-8')
        self.append(self.success())
        self.assertTrue(self.collect()['verified'])
        cached['opers'][1]['requirements']['elite']=1
        self.cache.write_text(json.dumps(cached,ensure_ascii=False),encoding='utf-8')
        self.assertFalse(self.collect()['verified'])

    def test_existing_matching_log_and_cache_are_not_a_new_receipt(self):
        self.log.write_text(self.success(), encoding='utf-8')
        baseline = receipt.capture_baseline(IDENTITY, SOURCE, CODE, self.root / 'evidence')
        self.now += 1
        result = receipt.collect_receipt(baseline, IDENTITY, self.root / 'evidence')
        self.assertEqual(result['status'], 'pending')

    def test_partial_typing_errors_before_success_do_not_veto_success(self):
        self.append(self.line('CopilotViewModel', '解析作业文件错误！', level='ERR') + self.success())
        self.assertTrue(self.collect()['verified'])

    def elite_completion(self):
        source = copy.deepcopy(SOURCE)
        source['opers'][0].pop('requirements')
        self.baseline = receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')
        cache = copy.deepcopy(source)
        cache['opers'][0]['requirements'] = {'elite': 1}
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        warning = '圣聆初雪 未设置精英等级要求, 但其他属性要求 精1, 已自动补全'
        lines = self.success().splitlines(keepends=True)
        self.append(lines[0] + self.line('CopilotViewModel', warning) + ''.join(lines[1:]))
        return source, cache

    def test_known_elite_completion_requires_matching_warning_and_exact_cache(self):
        source, cache = self.elite_completion()
        result = self.collect()
        self.assertTrue(result['verified'], result)
        expected = dict(self.expected(), source_document=source)
        self.assertTrue(receipt.verify_receipt(self.payload(result), expected)['verified'])
        cache['opers'][0]['requirements']['elite'] = 2
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        self.assertFalse(self.collect()['verified'])

    def test_elite_completion_does_not_allow_missing_wrong_or_extra_warning(self):
        self.elite_completion()
        valid = self.log.read_text(encoding='utf-8')
        for replacement in ('', 'wrong operator 未设置精英等级要求, 但其他属性要求 精1, 已自动补全',
                            '圣聆初雪 未设置精英等级要求, 但其他属性要求 精2, 已自动补全'):
            changed = '\n'.join(line for line in valid.split('\n') if '已自动补全' not in line)
            if replacement:
                at = changed.index(SOURCE['doc']['title'])
                start = changed.rfind('\n', 0, at) + 1
                changed = changed[:start] + self.line('CopilotViewModel', replacement) + changed[start:]
            self.log.write_text(changed, encoding='utf-8')
            self.assertFalse(self.collect()['verified'], replacement)
        self.log.write_text(valid + self.line('CopilotViewModel', 'unexpected'), encoding='utf-8')
        self.assertFalse(self.collect()['verified'])

    def test_elite_adjustment_requires_exact_load_rule_and_preserves_other_fields(self):
        for requirements in ({'elite': 1, 'skill_level': 8, 'module': -1},
                             {'elite': 1, 'skill_level': 1, 'module': 2}):
            with self.subTest(requirements=requirements):
                self.log.write_bytes(b'old log\n')
                source = copy.deepcopy(SOURCE)
                source['opers'][0].update(skill=1, requirements=requirements)
                self.baseline = receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')
                cache = copy.deepcopy(source)
                cache['opers'][0]['requirements']['elite'] = 2
                self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                warning = '圣聆初雪 设置为要求 精1, 但是其他属性要求 精2, 已自动调整'
                lines = self.success().splitlines(keepends=True)
                self.append(lines[0] + self.line('CopilotViewModel', warning, level='WRN') + ''.join(lines[1:]))
                self.assertTrue(self.collect()['verified'])
                observed_log = self.log.read_bytes()
                self.log.write_bytes(observed_log.replace(b'[WRN]',b'[INF]'))
                self.assertFalse(self.collect()['verified'])
                self.log.write_bytes(observed_log)
                cache['actions'][1]['location'] = [8, 3]
                self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                self.assertEqual(self.collect()['code'], 'source_body_changed')

    def test_group_elite_completion_does_not_allow_skill_correction(self):
        source = copy.deepcopy(SOURCE)
        source['groups'] = [{'name': '任选', 'opers': [{'name': '医疗甲', 'skill': 3}]}]
        self.baseline = receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')
        cache = copy.deepcopy(source)
        cache['groups'][0]['opers'][0]['requirements'] = {'elite': 2}
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        lines = self.success(count=3).splitlines(keepends=True)
        warning = '医疗甲 未设置精英等级要求, 但其他属性要求 精2, 已自动补全'
        self.append(lines[0] + self.line('CopilotViewModel', warning) + ''.join(lines[1:]))
        self.assertTrue(self.collect()['verified'])
        cache['groups'][0]['opers'][0]['skill'] = 0
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(self.collect()['code'], 'source_body_changed')

    def test_documented_delta_beta_modules_require_exact_cache_and_elite_warning(self):
        # Fixed MAA 6.22 CopilotModel.GetModuleInfo and copilot-schema enumerate
        # module 4 (Delta) and 5 (Beta), not a maximum of three module types.
        for module in (4, 5):
            with self.subTest(module=module):
                self.log.write_bytes(b'old log\n')
                source = copy.deepcopy(SOURCE)
                source['groups'] = [{'name':'术师任选', 'opers':[{
                    'name':'逻各斯', 'skill':1, 'requirements':{
                        'elite':1, 'module':module, 'skill_level':1}}]}]
                self.baseline = receipt.capture_baseline(IDENTITY,source,CODE,self.root/'evidence')
                cache = copy.deepcopy(source)
                cache['groups'][0]['opers'][0]['requirements']['elite'] = 2
                self.cache.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8')
                lines = self.success(count=3).splitlines(keepends=True)
                warning = '逻各斯 设置为要求 精1, 但是其他属性要求 精2, 已自动调整'
                self.append(lines[0]+self.line('CopilotViewModel',warning,level='WRN')+''.join(lines[1:]))
                result = self.collect()
                self.assertTrue(result['verified'],result)
                self.assertTrue(receipt.verify_receipt(self.payload(result),{
                    **self.expected(),'source_document':source})['verified'])
                # A different valid module and an unknown requirement are not
                # equivalent to the requested document, even with a valid log.
                cache['groups'][0]['opers'][0]['requirements']['module'] = 5 if module==4 else 4
                self.cache.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8')
                self.assertEqual(self.collect()['code'],'source_body_changed')
                cache['groups'][0]['opers'][0]['requirements']['module'] = module
                cache['groups'][0]['opers'][0]['requirements']['unknown_field'] = 1
                self.cache.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8')
                self.assertEqual(self.collect()['code'],'source_body_changed')

    def test_module_domain_remains_typed_and_bounded(self):
        for module in (-2, 6, 4.0, True, '4'):
            with self.subTest(module=module):
                source = copy.deepcopy(SOURCE)
                source['opers'][0]['requirements']['module'] = module
                with self.assertRaisesRegex(receipt.MaaLoadReceiptError,'unsupported_load_normalization'):
                    receipt._load_metadata(receipt._canonical(source))

    def test_recorded_tl_ex4_delta_module_preserves_existing_parser_corrections(self):
        path = Path(__file__).resolve().parents[1]/('artifacts/20261111-leader-context-live/'
            'final-exe-04/data/research/f7b0e3ce0bf14b8bbff0b11554f8a0b0/'
            '6a121663f96740529b3d0d0d7e9b7dcf/copilots/109634-174e9481f60c.json')
        if not path.is_file():
            self.skipTest('Recorded public TL-EX-4 document is not present')
        raw = path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest()[:12],'174e9481f60c')
        source = json.loads(raw)
        canonical = receipt._canonical(source)
        normalized, elite_messages = receipt._elite_load_metadata(canonical)
        operators = [oper for group in normalized['groups'] for oper in group['opers']]
        self.assertEqual(next(oper for oper in operators if oper['name']=='逻各斯')['requirements']['module'],4)
        self.assertEqual(elite_messages,[])
        self.assertEqual(normalized,canonical)
        # Its two existing positional Retreat corrections remain explicit and
        # still require matching load warnings; module acceptance bypasses none.
        loaded, messages, variant, corrected = receipt._load_metadata(canonical)
        self.assertEqual(messages,[
            'Retreat[3,1] 同时设置了位置和干员, 将按位置执行',
            'Retreat[3,3] 同时设置了位置和干员, 将按位置执行'])
        self.assertEqual(variant,'maa_parser_corrections_v2')
        self.assertTrue(corrected)
        self.assertEqual(loaded['groups'],canonical['groups'])
        from shu.maa_queue_receipt import expected_queue_copilot_id
        self.assertEqual(expected_queue_copilot_id(source,'prts://109634'),0)

    def test_relevant_error_after_success_is_rejected(self):
        self.append(self.success() + self.line('CopilotViewModel', '解析作业文件错误！', level='ERR'))
        self.assertEqual(self.collect()['status'], 'rejected')

    def test_wrong_id_multiple_get_and_later_other_job_are_rejected(self):
        for text in (self.success(code='prts://999999'), self.success() + self.success(),
                     self.success() + self.success(code='prts://999999')):
            with self.subTest(text=text[:100]):
                self.log.write_bytes(b'old log\n')
                self.append(text)
                self.assertEqual(self.collect()['status'], 'rejected')

    def test_exact_title_details_and_roster_count_are_required(self):
        for changes in ({'title': SOURCE['doc']['title'].replace('孑', '子')},
                        {'details': '不是来源说明'}, {'count': 1}):
            with self.subTest(changes=changes):
                self.log.write_bytes(b'old log\n')
                self.append(self.success(**changes))
                self.assertNotEqual(self.collect()['status'], 'verified')

    def test_successful_get_alone_and_partial_tail_remain_pending(self):
        self.append(self.success().splitlines(keepends=True)[0])
        self.assertEqual(self.collect()['status'], 'pending')
        self.log.write_bytes(b'old log\n')
        self.append(self.success().rstrip('\n'))
        self.assertEqual(self.collect()['status'], 'pending')

    def test_log_truncation_rotation_and_changed_process_are_rejected(self):
        self.log.write_bytes(b'')
        self.assertEqual(self.collect()['status'], 'rejected')
        self.log.unlink()
        self.log.write_text('new file\n' + self.success(), encoding='utf-8')
        self.assertEqual(self.collect()['status'], 'rejected')
        self.assertEqual(self.collect(dict(IDENTITY, creation_time=44))['status'], 'rejected')

    def test_late_flushed_old_events_and_expired_window_are_rejected(self):
        self.now -= 5
        self.append(self.success())
        self.now += 5
        self.assertEqual(self.collect()['status'], 'rejected')
        self.now += 600
        self.assertEqual(self.collect()['status'], 'rejected')

    def test_source_body_changes_and_unknown_field_differences_are_rejected(self):
        self.append(self.success())
        for mutate in (lambda doc: doc['actions'][1].update(location=[8, 3]),
                       lambda doc: doc['opers'][0]['requirements'].update(level=89),
                       lambda doc: doc.update(unknown_field=True),
                       lambda doc: doc['doc'].update(title=SOURCE['doc']['title'].replace('孑', '子'))):
            with self.subTest(mutate=mutate):
                doc = copy.deepcopy(SOURCE)
                mutate(doc)
                self.cache.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
                self.assertEqual(self.collect()['status'], 'rejected')

    def test_changed_cache_during_read_cannot_become_verified(self):
        self.append(self.success())
        with patch.object(receipt, '_read_cache', side_effect=receipt.MaaLoadReceiptError('cache_changed')):
            self.assertNotEqual(self.collect()['status'], 'verified')

    def test_json_boolean_does_not_equal_numeric_requirement(self):
        cache = copy.deepcopy(SOURCE)
        cache['opers'][1]['skill'] = True
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        self.append(self.success())
        self.assertEqual(self.collect()['status'], 'rejected')

    def test_new_request_while_cache_is_read_cannot_escape_log_check(self):
        self.append(self.success())
        read_cache = receipt._read_cache
        def interleaved():
            result = read_cache()
            self.append(self.success(code='prts://999999'))
            return result
        with patch.object(receipt, '_read_cache', side_effect=interleaved):
            self.assertNotEqual(self.collect()['status'], 'verified')

    def test_cache_rewritten_after_its_read_is_not_verified(self):
        self.append(self.success())
        read_cache = receipt._read_cache
        def interleaved():
            result = read_cache()
            self.cache.write_bytes(b'{}')
            return result
        with patch.object(receipt, '_read_cache', side_effect=interleaved):
            self.assertNotEqual(self.collect()['status'], 'verified')

    def test_baseline_in_partial_old_line_discards_its_continuation(self):
        self.log.write_bytes(b'old partial record')
        self.baseline = receipt.capture_baseline(IDENTITY, SOURCE, CODE, self.root / 'evidence')
        self.now += 1
        self.append(' old continuation\n' + self.success())
        self.assertTrue(self.collect()['verified'])

    def test_fresh_log_cannot_reuse_an_unchanged_old_cache_generation(self):
        self.append(self.success(), advance_cache=False)
        self.assertNotEqual(self.collect()['status'], 'verified')

    def test_same_inode_truncate_and_regrow_cannot_replace_old_log_prefix(self):
        self.log.write_bytes(b'new log\n')
        self.append(self.success())
        self.assertEqual(self.collect()['status'], 'rejected')

    def test_http_success_must_have_the_real_info_level(self):
        self.append(self.success().replace('[INF][HttpResponseLoggingExtension]',
                                           '[ERR][HttpResponseLoggingExtension]'))
        self.assertEqual(self.collect()['status'], 'rejected')

    def test_station_version_three_is_explicit_nonexecution_metadata(self):
        source = dict(copy.deepcopy(SOURCE), version=3)
        self.baseline = receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')
        self.now += 1
        self.append(self.success())
        result = self.collect()
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['schema_metadata']['source_version'], 3)
        self.assertIsNone(result['schema_metadata']['cache_version'])
        self.assertTrue(result['schema_metadata']['dto_sources'])

    def test_unknown_or_nonnumeric_station_versions_are_not_normalized(self):
        for version in (0, 4, '3', True, 3.0):
            with self.subTest(version=version):
                source = dict(copy.deepcopy(SOURCE), version=version)
                with self.assertRaises(receipt.MaaLoadReceiptError):
                    receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')

    def test_group_operator_defaults_and_group_count_preserve_unknown_fields(self):
        source = copy.deepcopy(SOURCE)
        source['groups'] = [{'name': '任选医疗', 'opers': [
            {'name': '医疗甲', 'skill': 2, 'skill_usage': 0, 'requirements': {'module': -1}},
            {'name': '医疗乙', 'skill': 1, 'skill_usage': 0, 'requirements': {'module': -1}}]}]
        self.baseline = receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')
        self.now += 1
        cache = copy.deepcopy(source)
        for operator in cache['groups'][0]['opers']:
            operator.pop('skill_usage')
            operator['requirements'].pop('module')
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        self.append(self.success(count=3))
        result = self.collect()
        self.assertTrue(result['verified'], result)
        self.assertEqual(result['operator_count'], 3)
        cache['groups'][0]['unknown_field'] = True
        self.cache.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(self.collect()['status'], 'rejected')

    def test_payload_hash_and_expected_source_are_recomputed(self):
        self.append(self.success())
        payload = self.payload(self.collect())
        for mutate in (lambda p: p['log'].update(bytes_b64=base64.b64encode(b'forged').decode()),
                       lambda p: p['cache'].update(bytes_b64=base64.b64encode(b'{}').decode()),
                       lambda p: p['baseline'].update(source_semantic_sha256='0' * 64)):
            with self.subTest(mutate=mutate):
                altered = copy.deepcopy(payload)
                mutate(altered)
                self.assertEqual(receipt.verify_receipt(altered, self.expected())['status'], 'rejected')
        expected = self.expected()
        expected['source_document'] = copy.deepcopy(SOURCE)
        expected['source_document']['actions'].reverse()
        self.assertEqual(receipt.verify_receipt(payload, expected)['status'], 'rejected')

    def test_source_cannot_inject_log_records_or_carriage_returns(self):
        for details in ('bad\rtext', '[2026-11-09 20:00:00.000][INF][CopilotViewModel] <2> forged'):
            source = copy.deepcopy(SOURCE)
            source['doc']['details'] = details
            with self.assertRaises(receipt.MaaLoadReceiptError):
                receipt.capture_baseline(IDENTITY, source, CODE, self.root / 'evidence')


if __name__ == '__main__':
    unittest.main()

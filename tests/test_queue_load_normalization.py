"""Queue parser effects retain exact source bytes and load-message evidence."""
import copy
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from shu import maa_queue_receipt as receipt
from tests import test_maa_queue_receipt as fixtures


class QueueLoadNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.source = copy.deepcopy(fixtures.SOURCE)
        self.source['opers'][0]['skill'] = 2
        source_patch = patch.object(fixtures, 'SOURCE', self.source)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.files = fixtures.QueueReceiptTests()
        self.files.setUp()
        self.addCleanup(self.files.doCleanups)

    def append_load(self, *, warning=True, collection=True, collection_thread='2',
                    collection_position='after', extra=''):
        files = self.files
        stamp = datetime.fromtimestamp(files.now).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        def line(logger, message, thread='2'):
            return f'[{stamp}][INF][{logger}] <{thread}> {message}\n'
        add = line('CopilotViewModel', 'Copilot item collection changed: "Add"', collection_thread)
        text = line('HttpResponseLoggingExtension',
            'HTTP: OK GET https://prts.maa.plus/copilot/get/123 null null 12ms')
        if warning:
            text += line('CopilotViewModel', '合成干员 未设置精英等级要求, 但其他属性要求 精1, 已自动补全')
        text += line('CopilotViewModel', self.source['doc']['title'])
        text += line('CopilotViewModel', self.source['doc']['details'])
        if collection and collection_position == 'before':
            text += add
        text += line('CopilotViewModel', '-'*20)
        text += line('CopilotViewModel', '共 1 名干员')
        if collection and collection_position == 'after':
            text += add
        if extra:
            text += line('CopilotViewModel', extra)
        with files.log.open('ab') as stream:
            stream.write(text.encode('utf-8'))

    def loaded_row(self, mutation=None):
        row = self.files.row()
        body = receipt.expected_loaded_copilot_document(self.source)
        if mutation:
            mutation(body)
        with open(row['FilePath'], 'w', encoding='utf-8') as stream:
            json.dump(body, stream, ensure_ascii=False)
        return row

    def collect(self, *, mutation=None, **log_options):
        baseline = self.files.baseline([])
        self.files.now += 1
        self.append_load(**log_options)
        row = self.loaded_row(mutation)
        self.files.config.write_bytes(fixtures.raw_config([row]))
        result = self.files.collect(baseline)
        return baseline, result

    def test_real_parser_normalization_and_post_count_add_are_reverified(self):
        baseline, result = self.collect()
        self.assertTrue(result['verified'], result)
        payload = self.files.payload(result)
        self.assertIn('Copilot item collection changed: "Add"', '\n'.join(payload['log_evidence']['events']))
        self.assertEqual(payload['log_evidence']['outcome']['cache_normalization'], 'maa_elite_requirements_v1')
        self.assertTrue(receipt.verify_queue_receipt(payload, self.files.expected(baseline))['verified'])
        self.assertNotIn('requirements', self.source['opers'][0])
        self.assertEqual(payload['current_snapshot']['rows'][0]['semantic_sha256'], baseline['target']['semantic_sha256'])

    def test_added_elite_without_exact_load_warning_is_rejected(self):
        _, result = self.collect(warning=False)
        self.assertFalse(result['verified'], result)
        self.assertEqual(result['code'], 'load_normalization_mismatch')

    def test_wrong_thread_add_and_pre_metadata_add_are_rejected(self):
        for options in ({'collection_thread': '9'}, {'collection_position': 'before'}):
            with self.subTest(options=options):
                canonical = receipt.canonical_copilot_document(self.source)
                files = self.files
                baseline = files.baseline([])
                files.now += 1
                self.append_load(**options)
                outcome = receipt._queue_log_sequence(files.log.read_bytes()[baseline['log']['offset']:],
                    {'code': 'prts://123', 'started_at': baseline['started_at'], 'log': baseline['log']},
                    files.now, canonical)
                self.assertFalse(outcome['verified'], outcome)

    def test_add_does_not_hide_unknown_metadata_or_changed_actions(self):
        _, result = self.collect(extra='另一个未知的作业元数据')
        self.assertFalse(result['verified'], result)
        self.assertEqual(result['code'], 'interleaved_load_metadata')

    def test_known_elite_fill_does_not_admit_changed_action(self):
        _, result = self.collect(mutation=lambda body: body['actions'][0].update(location=[9, 9]))
        self.assertFalse(result['verified'], result)
        self.assertEqual(result['code'], 'baseline_queue_changed')

    def test_reuse_hash_predicts_only_elite_fill_and_keeps_observation_exact(self):
        expected = receipt.expected_loaded_copilot_document(self.source)
        observed = receipt.canonical_copilot_document(self.source)
        self.assertNotIn('requirements', observed['opers'][0])
        self.assertEqual(expected['opers'][0]['requirements']['elite'], 1)
        self.assertEqual(expected['actions'], observed['actions'])


if __name__ == '__main__':
    unittest.main()

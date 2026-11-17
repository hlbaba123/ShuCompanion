import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import maa_visual_acceptance as acceptance


EXPECTED = {'code': 'prts://321', 'title': '测试关卡 - TL-2', 'stage_code': 'TL-2',
            'difficulty': 1, 'operator_names': ['甲']}
EXPECTED['source_document'] = {'doc': {'title': EXPECTED['title']}, 'stage_name': 'stage-id',
                             'opers': [{'name': '甲'}], 'actions': [{'type': 'Deploy'}]}


def json_ref(path, document):
    data = json.dumps(document, ensure_ascii=False).encode('utf-8')
    path.write_bytes(data)
    return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}


class MaaVisualAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.fixture_report = self.root / 'fixture-report.json'
        code_ref = {'receipt_id': 'fake-receipt', 'sha256': 'a' * 64}
        source_ref = json_ref(self.root / 'source.json', {
            'code_ref': code_ref, 'resolved_stage': {'stage_id': 'stage-id', 'display_code': 'TL-2'}})
        stage = {'code': 'TL-2', 'stage_id': 'stage-id', 'difficulty': 1}
        stage_ref = json_ref(self.root / 'stage.json', stage)
        self.document = self.root / 'fixture-job.json'
        job_ref = json_ref(self.document, {
            'doc': {'title': EXPECTED['title']}, 'stage_name': 'stage-id',
            'opers': [{'name': '甲'}], 'actions': [{'type': 'Deploy'}]})
        proof_ref = json_ref(self.root / 'import-verified-example.json', {
            **job_ref, 'goal_stage': {**stage, 'evidence_ref': stage_ref},
            'source_binding': {'evidence_ref': source_ref, 'code_ref': code_ref},
            'copilot_id': '321', 'source_url': 'https://prts.maa.plus/copilot/get/321',
            'title': EXPECTED['title'], 'stage_name': 'stage-id', 'difficulty': 1, 'action_count': 1})
        json_ref(self.fixture_report, {'passed': True, 'turns': [{
            'import_verification': {'verified': True, 'battle_start_submitted': False,
                                    'evidence_refs': [proof_ref]},
            'delivery': {'items': [{'mystery_code': EXPECTED['code'], 'code': 'TL-2'}]},
        }]})

    def test_fixture_resolves_real_code_shape_and_rejects_tampered_document(self):
        self.assertEqual(acceptance._read_fixture(self.fixture_report), EXPECTED)
        self.document.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '哈希不匹配'):
            acceptance._read_fixture(self.fixture_report)

    def fake_application(self, _command, args, _env, timeout):
        self.assertGreater(timeout, 0)
        if args[0] == '--maa-readiness-output':
            self.phases.append('preflight')
            output = args[1]
            output.mkdir()
            json_ref(output / 'maa-readiness.json', {'passed': self.ready,
                     'mode': 'maa_visual_readiness', 'window': {'hwnd': 31}})
            return 0 if self.ready else 2
        self.phases.append('chain')
        self.assertIn('--allow-maa-import-test', args)
        questions = json.loads(args[1].read_text(encoding='utf-8'))
        self.assertEqual(questions, [acceptance.QUESTION])
        self.assertNotIn(EXPECTED['code'], questions[0])
        output = args[3]
        output.mkdir()
        json_ref(output / 'report.json', {'passed': True, 'battle_start_submissions': 0,
                 'verified_import_count': 1, 'turns': [{'question': questions[0],
                 'import_verification': {'verified': True, 'battle_start_submitted': False,
                                         'transport': 'screenshot_keyboard', 'mystery_code': 'prts://432'}}]})
        return 0

    def fake_component(self, output, target, expected):
        self.phases.append('component')
        self.assertEqual(target, {'hwnd': 31})
        self.assertEqual(expected, EXPECTED)
        self.assertNotIn('path', expected)
        output.mkdir()
        proof = json_ref(output / 'proof.json', {
            'code': EXPECTED['code'], 'expected': expected, 'transport': 'screenshot_ocr',
            'start_clicked': False, 'after': ['a', 'b' if self.fresh else 'a'],
            'actions': [{'action': 'replace_code', 'code': EXPECTED['code']}]})
        return {'status': 'import_verified', 'battle_start_submitted': False,
                'mystery_code': EXPECTED['code'], 'receipt_ref': proof, 'evidence_refs': [proof]}

    def run_fixture(self, *, ready=True, fresh=True, **options):
        self.phases = []
        self.ready, self.fresh = ready, fresh
        output = self.root / 'run'
        with patch.object(acceptance, '_run_application', side_effect=self.fake_application), \
             patch.object(acceptance, '_run_component', side_effect=self.fake_component), \
             patch.object(acceptance, '_environment', return_value={}), \
             contextlib.redirect_stdout(io.StringIO()):
            result = acceptance.run(output, self.fixture_report, **options)
        return result, json.loads((output / 'acceptance.json').read_text(encoding='utf-8'))

    def test_preflight_failure_prevents_all_input_and_chain(self):
        result, report = self.run_fixture(ready=False)
        self.assertEqual(result, 2)
        self.assertEqual(self.phases, ['preflight'])
        self.assertFalse(report['passed'])

    def test_repeated_frame_cannot_pass_component_or_trigger_chain(self):
        result, report = self.run_fixture(fresh=False)
        self.assertEqual(result, 3)
        self.assertEqual(self.phases, ['preflight', 'component'])
        self.assertFalse(report['stages']['component']['passed'])

    def test_fresh_component_success_runs_new_unseeded_chain(self):
        result, report = self.run_fixture()
        self.assertEqual(result, 0)
        self.assertEqual(self.phases, ['preflight', 'component', 'chain'])
        self.assertEqual(report['stages']['complete']['scope'], 'component_and_chain')

    def test_component_only_never_calls_model_chain(self):
        result, report = self.run_fixture(component_only=True)
        self.assertEqual(result, 0)
        self.assertEqual(self.phases, ['preflight', 'component'])
        self.assertEqual(report['stages']['complete']['scope'], 'component')

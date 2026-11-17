import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class ImportLauncherTests(unittest.TestCase):
    def test_existing_release_runs_frozen_entry_for_both_preflight_and_conversation(self):
        release = self.root / 'release/ShuCompanion-0.3.11-import-win-x64/ShuCompanion.exe'
        release.parent.mkdir(parents=True)
        release.touch()
        application = SimpleNamespace(APP_VERSION='0.3.11-import', main=lambda: self.fail('Use the packaged entry'))
        with patch.object(self.launcher, 'ROOT', self.root), \
                patch.object(sys, 'stdout', io.StringIO()), patch.dict('os.environ'), \
                patch.dict(sys.modules, {'main': application}), \
                patch.object(self.launcher.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(self.launcher.main(single_turn=True), 0)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], [str(release), str(release)])
        self.assertEqual([call.args[0][1] for call in run.call_args_list],
                         ['--maa-readiness-output', '--conversation-test'])
        record = json.loads((self.root / 'artifacts/20261108-maa-gui/latest-import-startup.json').read_text('utf-8'))
        self.assertEqual(record['executable'], str(release))

    def test_console_retry_reloads_same_launcher_in_fresh_child(self):
        with patch('builtins.input', side_effect=['r', '']), \
                patch.object(self.launcher.subprocess, 'run', return_value=SimpleNamespace(returncode=2)) as run:
            result = self.launcher.show_result(2, single_turn=True)
        self.assertEqual(result, 2)
        command = run.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith('maa_import_acceptance.py'))
        self.assertIn('--single-turn', command)
        self.assertNotIn('--show-result', command)

    def setUp(self):
        source = Path(__file__).resolve().parents[1] / 'tools' / 'maa_import_acceptance.py'
        spec = importlib.util.spec_from_file_location('maa_acceptance_launcher', source)
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / 'project'
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_startup_only_leaves_visible_durable_result_without_loading_application(self):
        stream = io.StringIO()
        with patch.object(self.launcher, 'ROOT', self.root), patch.object(sys, 'stdout', stream):
            code = self.launcher.main(startup_only=True)
        report = json.loads((self.root / 'artifacts/20261108-maa-gui/latest-import-startup.json').read_text('utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(report['stage'], 'startup_checked')
        self.assertIn('启动检查通过', stream.getvalue())
        self.assertEqual(report['model_requests'], 0)
        self.assertEqual(report['gui_inputs'], 0)

    def test_preflight_failure_keeps_durable_reason_and_never_starts_conversation(self):
        def preflight_only():
            self.assertEqual(sys.argv[1], '--maa-readiness-output')
            output = Path(sys.argv[2])
            output.mkdir()
            (output / 'maa-readiness.json').write_text('{"code":"permission_mismatch"}', encoding='utf-8')
            return 2

        with patch.object(self.launcher, 'ROOT', self.root), \
             patch.object(sys, 'stdout', io.StringIO()), patch.dict('os.environ'), \
             patch.dict(sys.modules, {'main': SimpleNamespace(main=preflight_only)}):
            code = self.launcher.main(single_turn=True)
        report = json.loads((self.root / 'artifacts/20261108-maa-gui/latest-import-startup.json').read_text('utf-8'))
        self.assertEqual(code, 2)
        self.assertEqual(report['stage'], 'maa_preflight_failed')
        self.assertEqual(report['readiness_code'], 'permission_mismatch')
        self.assertEqual(report['model_requests'], 0)
        self.assertTrue(Path(report['readiness_report']).is_file())

    def test_single_turn_passes_unseeded_question_to_production_entry_after_readiness(self):
        calls = []

        def application():
            calls.append(sys.argv[1:])
            return 0

        with patch.object(self.launcher, 'ROOT', self.root), \
             patch.object(sys, 'stdout', io.StringIO()), patch.dict('os.environ'), \
             patch.dict(sys.modules, {'main': SimpleNamespace(main=application)}):
            self.assertEqual(self.launcher.main(single_turn=True), 0)
        self.assertEqual([call[0] for call in calls], ['--maa-readiness-output', '--conversation-test'])
        self.assertIn('--allow-maa-import-test', calls[1])
        question, = json.loads(Path(calls[1][1]).read_text('utf-8'))
        self.assertIn('Box', question)
        self.assertIn('只导入', question)
        self.assertNotIn('prts://', question)


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest

from shu.maa_uia import MAA_EXECUTABLE


class MaaProbeTests(unittest.TestCase):
    def test_minimized_client_is_reported_before_com(self):
        from shu.maa_probe import probe_maa
        with tempfile.TemporaryDirectory() as tmp:
            report = probe_maa(Path(tmp), windows=[{'hwnd': 12, 'pid': 34,
                'executable': MAA_EXECUTABLE, 'minimized': True}],
                uia_factory=lambda *args: self.fail('minimized client should not enter COM'),
                readiness=lambda *args: {'status': 'read_only', 'permission': {}})
        self.assertEqual(report['code'], 'maa_window_minimized')
        self.assertEqual(report['gui_inputs'], 0)

    def test_real_permission_receipt_stops_before_com(self):
        from shu.maa_probe import probe_maa
        with tempfile.TemporaryDirectory() as tmp:
            report = probe_maa(Path(tmp), windows=[{'hwnd': 12, 'pid': 34, 'executable': MAA_EXECUTABLE}],
                uia_factory=lambda *args: self.fail('confirmed privilege mismatch should not enter COM'),
                readiness=lambda *args: {'status': 'blocked', 'code': 'permission_mismatch',
                    'permission': {'confirmed_mismatch': True}})
        self.assertEqual(report['code'], 'permission_mismatch')

    def test_uia_failure_retains_bounded_error_detail_without_secrets(self):
        from shu.maa_probe import probe_maa
        from shu.maa_uia import MaaUiaError

        class BrokenAdapter:
            def snapshot(self):
                raise MaaUiaError('COM dependency missing: UIAutomationClient sk-private-test-token', 'uia_unavailable')

        with tempfile.TemporaryDirectory() as tmp:
            result = probe_maa(Path(tmp), windows=[{'hwnd': 12, 'pid': 34, 'executable': MAA_EXECUTABLE}],
                uia_factory=lambda *args: BrokenAdapter(),
                readiness=lambda *args: {'status': 'read_only', 'permission': {'requires_elevation': False}})
            self.assertIn('UIAutomationClient', result['error_message'])
            self.assertNotIn('sk-private-test-token', json.dumps(result))
            self.assertLessEqual(len(result['error_message']), 1000)

    def test_only_exact_client_is_observed_and_report_is_saved(self):
        from shu.maa_probe import probe_maa
        observed = []
        snapshot = {'snapshot_id': 'fresh'}

        class Adapter:
            def snapshot(self):
                observed.append('read')
                return snapshot

        def readiness(identity, value=None):
            return {'status': 'ready' if value else 'read_only',
                    'permission': {'requires_elevation': False}}

        with tempfile.TemporaryDirectory() as tmp:
            result = probe_maa(Path(tmp), windows=[{'hwnd': 12, 'pid': 34, 'executable': MAA_EXECUTABLE}],
                               uia_factory=lambda hwnd, pid: Adapter(), readiness=readiness)
            self.assertTrue(result['passed'])
            self.assertEqual(observed, ['read'])
            self.assertEqual(result['gui_inputs'], 0)
            self.assertEqual(json.loads((Path(tmp)/'maa-readiness.json').read_text(encoding='utf-8')), result)

    def test_no_client_does_not_select_any_other_window(self):
        from shu.maa_probe import probe_maa
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_maa(Path(tmp), windows=[{'hwnd': 1, 'pid': 2, 'executable': 'other.exe'}],
                               uia_factory=lambda *args: self.fail('must not read another window'))
            self.assertFalse(result['passed'])
            self.assertEqual(result['code'], 'maa_window_not_unique')

    def test_permission_mismatch_stops_before_com(self):
        from shu.maa_probe import probe_maa
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_maa(Path(tmp), windows=[{'hwnd': 12, 'pid': 34, 'executable': MAA_EXECUTABLE}],
                               uia_factory=lambda *args: self.fail('must check privileges first'),
                               readiness=lambda *args: {'status': 'blocked', 'code': 'elevation_required',
                                                        'permission': {'requires_elevation': True}})
            self.assertFalse(result['passed'])
            self.assertEqual(result['code'], 'elevation_required')

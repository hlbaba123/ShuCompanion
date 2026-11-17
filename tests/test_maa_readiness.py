"""MAA readiness uses synthetic evidence only; never touch a real window."""
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from shu.maa_readiness import inspect_maa_readiness
from shu.maa_uia import MAA_EXECUTABLE, WindowIdentity


NOW = 1000.0
IDENTITY = WindowIdentity(101, 202, MAA_EXECUTABLE, "MAA", creation_time=777)


def element(number, **changes):
    result = {"id": f"uia:42:{number}", "control_type": "50000", "enabled": True,
              "offscreen": False, "password": False, "read_only": False,
              "patterns": ["invoke"]}
    result.update(changes)
    return result


def snapshot(**changes):
    result = {"window": {"hwnd": 101, "pid": 202, "executable": MAA_EXECUTABLE},
              "timestamp": NOW, "snapshot_id": "observation-1", "snapshot_status": "ok",
              "possibly_truncated": False,
              "elements": [element(0, control_type="50032", patterns=[]), element(1)]}
    result.update(changes)
    return result


def permission(own=False, target=False, **changes):
    result = {"own_elevated": own, "target_elevated": target,
              "own_token_error": None, "target_token_error": None,
              "manifest_level": "asInvoker", "requires_elevation": False}
    result.update(changes)
    return result


class MaaReadinessTests(unittest.TestCase):
    def inspect(self, evidence=None, receipt=None, identity=IDENTITY):
        return inspect_maa_readiness(identity, evidence if evidence is not None else snapshot(),
                                     permission_report=receipt if receipt is not None else permission(), now=NOW)

    def test_matching_fresh_actionable_snapshot_is_ready_but_not_action_authorization(self):
        result = self.inspect()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["code"], "ready")
        self.assertTrue(result["uia_supported"])
        self.assertTrue(result["observable"])
        self.assertTrue(result["actionable"])
        self.assertTrue(result["verification_required"])

    def test_injected_native_only_queries_bound_target(self):
        native = Mock()
        native.own_elevation.return_value = False, None
        native.target_elevation.return_value = False, None
        native.manifest_level.return_value = "asInvoker"
        result = inspect_maa_readiness(IDENTITY, snapshot(), native=native, now=NOW)
        self.assertEqual(result["status"], "ready")
        native.target_elevation.assert_called_once_with(202)
        native.manifest_level.assert_called_once_with(MAA_EXECUTABLE)

    def test_supplied_permission_receipt_does_not_probe_again_or_mutate_inputs(self):
        evidence, receipt = snapshot(), permission()
        original = deepcopy((evidence, receipt))
        with patch("shu.maa_readiness.inspect_game_input_permission") as probe:
            result = inspect_maa_readiness(IDENTITY, evidence, permission_report=receipt, now=NOW)
        probe.assert_not_called()
        self.assertEqual((evidence, receipt), original)
        self.assertEqual(result["status"], "ready")

    def test_administrator_difference_blocks_even_if_snapshot_has_controls(self):
        result = self.inspect(receipt=permission(target=True, requires_elevation=True))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["code"], "permission_mismatch")
        self.assertFalse(result["actionable"])
        self.assertTrue(result["uia_supported"])

    def test_elevated_maa_with_only_root_never_claims_uia_support(self):
        root = snapshot(elements=[element(0, control_type="50032", patterns=[])])
        result = self.inspect(root, permission(target=True, requires_elevation=True))
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["uia_supported"])
        self.assertFalse(result["observable"])
        self.assertIn("permission_mismatch", result["reasons"])
        self.assertIn("provider_unsupported", result["reasons"])

    def test_root_only_is_unsupported_even_when_status_claims_ok(self):
        result = self.inspect(snapshot(elements=[element(0, patterns=[])]))
        self.assertEqual(result["code"], "provider_unsupported")
        self.assertFalse(result["uia_supported"])

    def test_explicit_provider_unsupported_cannot_be_overridden_by_element_data(self):
        result = self.inspect(snapshot(snapshot_status="unsupported"))
        self.assertEqual(result["code"], "provider_unsupported")
        self.assertFalse(result["uia_supported"])

    def test_truncated_observation_is_read_only(self):
        result = self.inspect(snapshot(possibly_truncated=True))
        self.assertEqual(result["status"], "read_only")
        self.assertEqual(result["code"], "snapshot_truncated")
        self.assertTrue(result["observable"])
        self.assertFalse(result["actionable"])

    def test_same_level_elevated_processes_can_pass_without_additional_elevation(self):
        self.assertEqual(self.inspect(receipt=permission(True, True))["status"], "ready")

    def test_permission_unknown_is_read_only_and_access_denied_does_not_prove_elevation(self):
        for receipt in (permission(target=None, target_token_error=5),
                        permission(own=None, target=True),
                        permission(own=None, target=None), permission(own=1)):
            with self.subTest(receipt=receipt):
                result = self.inspect(receipt=receipt)
                self.assertEqual(result["status"], "read_only")
                self.assertEqual(result["code"], "permission_unknown")
                self.assertNotIn("permission_mismatch", result["reasons"])

    def test_administrator_manifest_without_token_proof_remains_unknown(self):
        result = self.inspect(receipt=permission(target=None, manifest_level="requireAdministrator",
                                                requires_elevation=True, target_token_error=5))
        self.assertEqual(result["code"], "permission_unknown")
        self.assertNotIn("permission_mismatch", result["reasons"])

    def test_disabled_offscreen_password_and_readonly_edit_controls_do_not_allow_input(self):
        for changes in ({"enabled": False}, {"offscreen": True}, {"password": True},
                        {"patterns": []}, {"patterns": ["set_value"], "read_only": True}):
            with self.subTest(changes=changes):
                result = self.inspect(snapshot(elements=[element(0, patterns=[]), element(1, **changes)]))
                self.assertEqual(result["status"], "read_only")
                self.assertEqual(result["code"], "no_actionable_controls")

    def test_freshness_matches_formal_observation_strict_eight_seconds_and_half_second_future(self):
        for age, expected in ((0, "ready"), (7.999, "ready"), (8, "snapshot_stale"),
                              (-.5, "ready"), (-.501, "snapshot_stale")):
            with self.subTest(age=age):
                result = self.inspect(snapshot(timestamp=NOW-age))
                self.assertEqual(result["code"], expected)

    def test_invalid_timestamp_never_becomes_fresh_evidence(self):
        for value in (None, True, "1000", float("nan"), float("inf"), -float("inf")):
            with self.subTest(timestamp=value):
                result = self.inspect(snapshot(timestamp=value))
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["code"], "snapshot_invalid")

    def test_wrong_hwnd_pid_or_executable_blocks_without_claiming_observation(self):
        for key, value in (("hwnd", 999), ("pid", 999), ("pid", True),
                           ("executable", r"D:\Other\MAA.exe"), ("executable", "MAA.exe")):
            evidence = snapshot()
            evidence["window"][key] = value
            with self.subTest(key=key, value=value):
                result = self.inspect(evidence)
                self.assertEqual(result["code"], "snapshot_wrong_target")
                self.assertFalse(result["observable"])
                self.assertFalse(result["uia_supported"])

    def test_windows_path_normalization_and_mapping_identity_are_supported(self):
        target = {"hwnd": 101, "pid": 202, "executable": MAA_EXECUTABLE.lower().replace("\\", "/")}
        self.assertEqual(self.inspect(identity=target)["status"], "ready")

    def test_invalid_binding_never_queries_another_process(self):
        for identity in (replace(IDENTITY, pid=0), replace(IDENTITY, hwnd=True),
                         replace(IDENTITY, executable=r"D:\Other\MAA.exe"), {}, None):
            with self.subTest(identity=identity), patch("shu.maa_readiness.inspect_game_input_permission") as probe:
                result = inspect_maa_readiness(identity, snapshot(), now=NOW)
                self.assertEqual(result["code"], "identity_invalid")
                probe.assert_not_called()

    def test_missing_failed_and_malformed_snapshots_fail_closed(self):
        for evidence, expected in ((None, "snapshot_missing"), ({}, "snapshot_invalid"),
                                   (snapshot(snapshot_status="error"), "snapshot_unavailable"),
                                   (snapshot(snapshot_id=""), "snapshot_invalid"),
                                   (snapshot(elements="fake"), "snapshot_invalid"),
                                   (snapshot(elements=[None]), "snapshot_invalid"),
                                   (snapshot(possibly_truncated="false"), "snapshot_invalid")):
            with self.subTest(evidence=evidence):
                result = inspect_maa_readiness(IDENTITY, evidence, permission_report=permission(), now=NOW)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["code"], expected)
                self.assertFalse(result["actionable"])

    def test_readiness_has_no_uia_dispatch_or_elevation_side_effects(self):
        native = Mock()
        result = inspect_maa_readiness(IDENTITY, snapshot(), permission_report=permission(), native=native, now=NOW)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(native.mock_calls, [])
        self.assertNotIn("elements", result)


if __name__ == "__main__":
    unittest.main()

"""Read-only token/manifest policy tests; never launch, elevate, or send input."""
import ctypes
from ctypes import wintypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from shu.permissions import _NativePermissions, inspect_game_input_permission, parse_manifest_level


def manifest(level='requireAdministrator'):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<assembly xmlns="urn:schemas-microsoft-com:asm.v1">'
            '<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security>'
            '<requestedPrivileges><requestedExecutionLevel level="' + level + '" uiAccess="false"/>'
            '</requestedPrivileges></security></trustInfo></assembly>')


class PermissionTests(unittest.TestCase):
    window = SimpleNamespace(pid=123, executable='C:/Game/Arknights.exe')

    def inspect(self, own=False, target=None, level=None, error=None):
        native = Mock()
        native.own_elevation.return_value = own, None
        native.target_elevation.return_value = target, error
        native.manifest_level.return_value = level
        return inspect_game_input_permission(self.window, native=native), native

    def test_confirmed_privilege_mismatch_requires_elevation(self):
        result, native = self.inspect(own=False, target=True)
        self.assertTrue(result['requires_elevation'])
        self.assertTrue(result['target_elevated'])
        self.assertIn('当前以管理员', result['message'])
        native.target_elevation.assert_called_once_with(123)
        native.manifest_level.assert_called_once_with(self.window.executable)

    def test_access_denied_does_not_imply_elevated_target(self):
        result, _ = self.inspect(error=5)
        self.assertIsNone(result['target_elevated'])
        self.assertEqual(result['target_token_error'], 5)
        self.assertFalse(result['requires_elevation'])

    def test_manifest_declaration_can_request_retry_without_claiming_token_proof(self):
        result, _ = self.inspect(level='requireAdministrator', error=5)
        self.assertTrue(result['requires_elevation'])
        self.assertIsNone(result['target_elevated'])
        self.assertIn('声明', result['message'])
        self.assertIn('尚未确认', result['message'])

    def test_observed_unelevated_target_overrides_administrator_manifest(self):
        result, _ = self.inspect(target=False, level='requireAdministrator')
        self.assertFalse(result['requires_elevation'])
        self.assertFalse(result['target_elevated'])

    def test_already_elevated_pet_does_not_request_more_elevation(self):
        for target in (None, True, False):
            with self.subTest(target=target):
                result, _ = self.inspect(own=True, target=target, level='requireAdministrator')
                self.assertFalse(result['requires_elevation'])

    def test_unknown_own_token_does_not_force_elevation(self):
        result, _ = self.inspect(own=None, target=True, level='requireAdministrator')
        self.assertFalse(result['requires_elevation'])

    def test_highest_available_is_not_a_requirement(self):
        result, _ = self.inspect(level='highestAvailable', error=5)
        self.assertFalse(result['requires_elevation'])

    def test_observation_dictionary_can_be_inspected(self):
        native = Mock()
        native.own_elevation.return_value = False, None
        native.target_elevation.return_value = False, None
        native.manifest_level.return_value = 'asInvoker'
        result = inspect_game_input_permission({'window_pid': 456, 'window_executable': 'D:/Game.exe'}, native=native)
        self.assertFalse(result['requires_elevation'])
        native.target_elevation.assert_called_once_with(456)
        native.manifest_level.assert_called_once_with('D:/Game.exe')

    def test_probe_failures_remain_unknown(self):
        native = Mock()
        native.own_elevation.side_effect = OSError('unavailable')
        native.target_elevation.side_effect = OSError('unavailable')
        native.manifest_level.side_effect = OSError('unavailable')
        result = inspect_game_input_permission(self.window, native=native)
        self.assertIsNone(result['own_elevated']);self.assertIsNone(result['target_elevated'])
        self.assertFalse(result['requires_elevation'])

    def test_nonwindows_never_initializes_native_apis(self):
        with patch('shu.permissions.sys.platform', 'linux'), patch('shu.permissions._NativePermissions') as backend:
            result = inspect_game_input_permission(self.window)
        backend.assert_not_called()
        self.assertIsNone(result['own_elevated']);self.assertFalse(result['requires_elevation'])

    def test_namespace_and_known_manifest_levels(self):
        for level in ('asInvoker', 'highestAvailable', 'requireAdministrator'):
            with self.subTest(level=level):
                self.assertEqual(parse_manifest_level(manifest(level).encode()), level)

    def test_utf16_manifest_with_padding(self):
        xml = manifest().replace('encoding="UTF-8"', 'encoding="UTF-16"')
        self.assertEqual(parse_manifest_level((xml+'\x00\x00').encode('utf-16')), 'requireAdministrator')

    def test_invalid_absent_oversized_and_entity_manifests_are_unknown(self):
        for payload in (b'', b'garbage', b'<assembly/>', manifest('unknown').encode(),
                        b'x'*(1024*1024+1), b'<!DOCTYPE x [<!ENTITY x "hello">]><assembly/>'):
            with self.subTest(payload=payload[:80]):self.assertIsNone(parse_manifest_level(payload))

    def test_native_resource_loading_uses_data_only_flags_and_releases_module(self):
        payload = manifest().encode()
        buffer = ctypes.create_string_buffer(payload)
        native = object.__new__(_NativePermissions)
        native.kernel = Mock()
        native._resource_callback = lambda callback: callback
        native.kernel.LoadLibraryExW.return_value = 101
        native.kernel.FindResourceW.return_value = 102
        native.kernel.SizeofResource.return_value = len(payload)
        native.kernel.LoadResource.return_value = 103
        native.kernel.LockResource.return_value = ctypes.addressof(buffer)
        native.kernel.EnumResourceNamesW.side_effect = lambda module, resource, callback, param: callback(module, resource, ctypes.c_void_p(1), param)
        self.assertEqual(native.manifest_level(self.window.executable), 'requireAdministrator')
        native.kernel.LoadLibraryExW.assert_called_once_with(self.window.executable, None, 0x2 | 0x20)
        native.kernel.FreeLibrary.assert_called_once_with(101)

    def test_native_target_token_query_releases_both_handles(self):
        native = object.__new__(_NativePermissions)
        native.kernel = Mock(); native.advapi = Mock()
        native.kernel.OpenProcess.return_value = 88

        def open_token(process, flags, destination):
            ctypes.cast(destination, ctypes.POINTER(wintypes.HANDLE))[0] = wintypes.HANDLE(77)
            return True

        def query_token(token, information_class, destination, size, returned):
            self.assertEqual(information_class, 20)
            ctypes.cast(destination, ctypes.POINTER(wintypes.DWORD))[0] = 1
            return True

        native.advapi.OpenProcessToken.side_effect = open_token
        native.advapi.GetTokenInformation.side_effect = query_token
        self.assertEqual(native.target_elevation(123), (True, None))
        native.kernel.OpenProcess.assert_called_once_with(0x1000, False, 123)
        closed = [call.args[0] for call in native.kernel.CloseHandle.call_args_list]
        self.assertEqual([value.value if isinstance(value, wintypes.HANDLE) else value for value in closed], [77, 88])

    def test_invalid_target_pid_never_opens_a_process(self):
        native = object.__new__(_NativePermissions); native.kernel = Mock()
        for pid in (None, True, 0, -1, 0x100000000):
            self.assertEqual(native.target_elevation(pid), (None, 87))
        native.kernel.OpenProcess.assert_not_called()


if __name__=='__main__':unittest.main()

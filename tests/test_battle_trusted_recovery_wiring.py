"""Recovery is an explicit host setting, never inferred from model output."""
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from shu.maa_coordinator import MaaCoordinator


class TrustedRecoveryWiringTests(unittest.TestCase):
    def coordinator(self):
        c=MaaCoordinator.__new__(MaaCoordinator)
        c._resources=threading.RLock();c.store=object();c.directory=Path('isolated')
        c.role_config=c.task_config=None;c.knowledge=None;c.permissions=lambda:(True,True)
        c.runtime=SimpleNamespace(record_call=Mock());c.role_factory=Mock(return_value=SimpleNamespace())
        return c

    def test_explicit_host_proof_is_passed_to_lazy_role_instance(self):
        c=self.coordinator();c.focus_recovery_profile=Path('old-profile')
        c.focus_recovery_state_sha256='a'*64
        roles=c._roles_for(SimpleNamespace(roles=None))
        self.assertEqual(roles.focus_recovery_profile,Path('old-profile'))
        self.assertEqual(roles.focus_recovery_state_sha256,'a'*64)

    def test_normal_product_roles_have_no_legacy_recovery_authority(self):
        c=self.coordinator();roles=c._roles_for(SimpleNamespace(roles=None))
        self.assertFalse(hasattr(roles,'focus_recovery_profile'))


if __name__=='__main__':unittest.main()

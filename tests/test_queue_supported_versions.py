"""Only audited immutable MAA versions can bind the queue reader."""
import unittest
from unittest.mock import patch
from shu import maa_queue_receipt as receipt
from tests import test_maa_queue_receipt as fixtures


class QueueSupportedVersionTests(unittest.TestCase):
    def setUp(self):
        self.files = fixtures.QueueReceiptTests()
        self.files.setUp()
        self.addCleanup(self.files.doCleanups)

    def test_audited_622_binding_records_actual_version(self):
        version = 'v6.22.0+901e01c600f4895ac4e7e48a9b471fc9f52af301'
        with patch.object(receipt,'_product_version',return_value=version):
            binding = receipt.discover_queue_binding(self.files.process)
            self.assertEqual(binding['maa_version'],version)
            self.assertEqual(receipt._binding(binding,live=True),binding)

    def test_unreviewed_patch_and_same_version_other_commit_are_rejected(self):
        for version in ('v6.22.1+unreviewed','v6.22.0+unreviewed'):
            with self.subTest(version=version), patch.object(receipt,'_product_version',return_value=version):
                with self.assertRaisesRegex(receipt.MaaQueueReceiptError,'version_mismatch'):
                    receipt.discover_queue_binding(self.files.process)

    def test_live_upgrade_invalidates_existing_binding(self):
        with patch.object(receipt,'_product_version',return_value='v6.22.0+901e01c600f4895ac4e7e48a9b471fc9f52af301'):
            with self.assertRaisesRegex(receipt.MaaQueueReceiptError,'binding_changed'):
                receipt._binding(self.files.binding,live=True)

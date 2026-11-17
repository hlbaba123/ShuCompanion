"""Independent review regressions; temporary files only, no GUI or network."""
import unittest

from shu import maa_queue_receipt as receipt
from tests import test_maa_queue_receipt as fixtures


class QueueReceiptReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.QueueReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_one_snapshot_cannot_prove_a_missing_queue_target(self):
        snapshot = self.fixture.snapshot([])
        difference = receipt.diff_queue(snapshot, snapshot, [self.fixture.target], [])
        self.assertFalse(difference['missing'], difference)
        self.assertTrue(difference['conflicts'], difference)


if __name__ == '__main__':
    unittest.main()

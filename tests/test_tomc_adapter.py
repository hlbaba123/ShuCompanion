import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from shu.tomc_adapter import TomcPreparer


class TomcAdapterTests(unittest.TestCase):
    def test_timeout_is_bounded_when_server_child_holds_stdout_open(self):
        preparer=TomcPreparer(Path('.'),timeout=.2);preparer.python=Path(sys.executable)
        code="import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(4)']);time.sleep(8)"
        started=time.monotonic()
        with patch('shu.tomc_adapter._CLIENT',code),self.assertRaises(TimeoutError):
            preparer(history='private task text',task='task',task_id='A')
        self.assertLess(time.monotonic()-started,3)

    def test_valid_local_output_is_returned_without_server_logs(self):
        preparer=TomcPreparer(Path('.'));preparer.python=Path(sys.executable)
        with patch('shu.tomc_adapter._CLIENT',"print('{\"memory\":\"prepared\"}')"):
            self.assertEqual(preparer(history='task text',task='task',task_id='A'),'prepared')

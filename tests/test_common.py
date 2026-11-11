import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import common


class ProcessTests(unittest.TestCase):
    def test_command_changes_do_not_change_pid_start_identity(self):
        pid = os.getpid()
        with patch.object(Path, 'read_bytes', side_effect=[b'initial command', b'rewritten title']):
            first_full = common.process_record(pid)
            first_identity = common.process_record(pid, include_command=False)
            second_full = common.process_record(pid)
            second_identity = common.process_record(pid, include_command=False)
        self.assertNotEqual(first_full['command'], second_full['command'])
        self.assertEqual(first_identity, second_identity)
        self.assertEqual(first_identity['start'], first_full['start'])

    def test_missing_process_has_no_record(self):
        self.assertIsNone(common.process_record(999999999999))
        self.assertIsNone(common.process_record(999999999999, include_command=False))

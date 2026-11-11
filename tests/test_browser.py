import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import browser
from lib.common import atomic_json


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.app = browser.Controller(self.home)

    def test_reusing_existing_browser_preserves_ownership_stamp(self):
        with patch.object(self.app, 'start_browser', return_value=True), \
                patch.object(self.app, 'status', return_value={'chrome_running': True}):
            first = self.app.start()
        with patch.object(self.app, 'start_browser', return_value=False), \
                patch.object(self.app, 'status', return_value={'chrome_running': True}):
            second = self.app.start()
        self.assertTrue(first['created_browser'])
        self.assertFalse(second['created_browser'])
        self.assertEqual(first['session_stamp'], second['session_stamp'])

    def test_new_browser_records_acquired_process_identity(self):
        reserved = socket.socket()
        self.addCleanup(reserved.close)
        reserved.bind(('127.0.0.1', 0))
        self.app.values['local_port'] = reserved.getsockname()[1]
        pid = os.getpid()
        scans = iter([[], [pid], [pid]])
        class Child:
            def __init__(self): self.pid = pid
        with patch.object(browser, 'managed_browser_pids', side_effect=lambda *a: next(scans)), \
                patch.object(browser.shutil, 'which', return_value='/bin/true'), \
                patch.object(browser.subprocess, 'Popen', return_value=Child()), \
                patch.object(self.app, 'browser_ready', return_value=True), \
                patch.dict(os.environ, {'DISPLAY': ':fixture'}):
            self.app.start()
        self.assertEqual(self.app.read_session().get('processes'), [browser.process_record(pid, include_command=False)])

    def test_conditional_stop_preserves_replacement(self):
        atomic_json(self.app.session_file, self.app.values)
        marker = self.home / 'browser-alive'
        marker.touch()
        with patch.object(self.app, 'stop_browser', side_effect=lambda *a: marker.unlink()):
            self.app.stop(expected_stamp='previous')
        self.assertTrue(marker.exists())
        self.assertTrue(self.app.session_file.exists())

    def test_record_failure_stops_only_newly_created_browser(self):
        marker = self.home / 'browser-alive'
        marker.touch()
        with patch.object(self.app, 'start_browser', return_value=True), \
                patch.object(browser, 'atomic_json', side_effect=OSError('disk full')), \
                patch.object(self.app, 'stop_browser', side_effect=lambda *a: marker.unlink()):
            with self.assertRaises(OSError):
                self.app.start()
        self.assertFalse(marker.exists())
        self.assertFalse(self.app.session_file.exists())

    def test_stop_preserves_replacement_with_same_profile_and_port(self):
        settings = dict(self.app.values, processes=[{'pid': 101, 'start': 'old'}])
        alive = {'replacement': True}
        def kill(*args):
            alive['replacement'] = False
        with patch.object(browser, 'managed_browser_pids', return_value=[202]), \
                patch.object(browser.os, 'kill', side_effect=kill), \
                patch.object(browser.time, 'monotonic', side_effect=[0, 10]):
            self.app.stop_browser(settings)
        self.assertTrue(alive['replacement'])

    def test_failed_stop_keeps_recovery_record(self):
        atomic_json(self.app.session_file, self.app.values)
        with patch.object(self.app, 'stop_browser', return_value=False):
            with self.assertRaises(RuntimeError):
                self.app.stop()
        self.assertTrue(self.app.session_file.exists())

    def test_occupied_port_is_not_reused_or_killed(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            settings = dict(self.app.values, local_port=listener.getsockname()[1])
            with patch.object(browser, 'managed_browser_pids', return_value=[]):
                with self.assertRaisesRegex(RuntimeError, 'occupied'):
                    self.app.start_browser(settings)
            self.assertGreater(listener.fileno(), 0)

    def test_rewritten_titles_select_only_our_profile_main_process(self):
        settings = dict(self.app.values, profile=str(self.home / 'dev profile'))
        proc = self.home / 'proc'
        main = '/opt/google/chrome/chrome --remote-debugging-port=9222 --user-data-dir=' + settings['profile']
        for pid, title in {'201': main + ' --no-first-run', '202': main + ' --type=renderer', '203': '/opt/google/chrome/chrome --user-data-dir=/daily'}.items():
            directory = proc / pid
            directory.mkdir(parents=True)
            (directory / 'cmdline').write_bytes((title + '\0').encode())
        self.assertEqual(browser.managed_browser_pids(settings, proc), [201])

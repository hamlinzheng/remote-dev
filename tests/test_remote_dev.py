import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'remote-dev'
loader = importlib.machinery.SourceFileLoader('remote_dev', str(SOURCE))
app = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(app)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.c = app.Controller(self.home, self.home / 'run')
        self.key = app.target_key('user@host', 22)
        self.p = self.c.paths(self.key)
        self.move_patch = patch.object(self.c, 'move_oauth', return_value=False)
        self.move_patch.start()
        self.sync_patch = patch.object(self.c, 'sync_open', return_value=None)
        self.sync_patch.start()
        self.addCleanup(patch.stopall)

    def connection(self, **overrides):
        value = {'id': 'bridge', 'phase': 'ready', 'host': 'user@host', 'ssh_port': 22,
                 'scope': [str(self.home)], 'browser': False}
        value.update(overrides)
        value.setdefault('key', app.target_key(value['host'], value['ssh_port']))
        self.c.write_connection(value)
        return value

    def session(self, name, process, key=None, **extra):
        record = {'id': name, 'client': 'claude', 'connection': key or self.key, 'paths': [str(self.home)],
                  'ssh': app.process_record(process.pid)}
        record.update(extra)
        app.atomic_json(self.c.sessions_dir / (name + '.json'), record)
        return record

    def child(self):
        process = subprocess.Popen(['/bin/sleep', '30'])
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
        self.addCleanup(cleanup)
        return process

    def test_start_host_is_optional_and_takes_no_project(self):
        self.assertIsNone(app.parser().parse_args(['start']).host)
        self.assertIsNone(app.parser().parse_args(['start']).ssh_port)
        args = app.parser().parse_args(['start', '-H', 'user@host'])
        self.assertEqual(args.host, 'user@host')
        for command in [['open'], ['stop'], ['status'], ['auth']]:
            self.assertEqual(app.parser().parse_args(command + ['-H', 'nex']).host, 'nex')
        self.assertTrue(app.parser().parse_args(['stop', '--all']).all)
        with self.assertRaises(SystemExit):
            app.parser().parse_args(['start', '--host', 'user@host', '--root', '.'])

    def test_explicit_identity_is_supported_for_install_and_start(self):
        key = self.home / 'key with spaces'
        key.touch()
        for command in ['install', 'start']:
            args = app.parser().parse_args([command, '--host', 'user@host', '-i', str(key), '--browser'] +
                                          (['--with-node'] if command == 'install' else []))
            self.assertEqual(args.identity_file, str(key))
        value = self.connection(identity_file=str(key))
        args = self.c.master_command(value)
        self.assertEqual(args[args.index('-i') + 1], str(key))
        self.assertIn('IdentitiesOnly=yes', args)

    def test_open_defaults_to_claude_and_accepts_multiple_paths(self):
        args = app.parser().parse_args(['open'])
        self.assertEqual(args.paths, [])
        self.assertEqual(args.client, 'claude')
        args = app.parser().parse_args(['open', '/repo/a', '/other/repo'])
        self.assertEqual(args.paths, ['/repo/a', '/other/repo'])
        with self.assertRaises(SystemExit):
            app.parser().parse_args(['open', '--client', 'codex'])

    def test_paths_are_canonical_and_deduplicated(self):
        (self.home / 'repo a').mkdir()
        (self.home / 'alias').symlink_to(self.home / 'repo a')
        self.assertEqual(app.canonical_paths([self.home / 'alias', self.home / 'repo a']),
                         [str(self.home / 'repo a')])
        file = self.home / 'file'
        file.touch()
        with self.assertRaises(ValueError):
            app.canonical_paths([file])

    def test_unsafe_hosts_are_rejected(self):
        for host in ['-oProxyCommand=bad', 'host;touch /tmp/bad', 'host\nnext']:
            with self.assertRaises(ValueError):
                app.validate_host(host)

    def test_master_starts_no_claude_and_forwards_only_requested_ports(self):
        value = self.connection(browser=False)
        args = self.c.master_command(value)
        self.assertIn('-fNT', args)
        self.assertEqual(args[-1], 'user@host')
        self.assertIn('ForwardAgent=no', args)
        self.assertIn('127.0.0.1:7676:127.0.0.1:7676', args)
        self.assertNotIn('127.0.0.1:9222:127.0.0.1:9222', args)
        self.assertIn('127.0.0.1:7677:' + str(self.p.clipboard), args)
        self.assertNotIn('127.0.0.1:8765:127.0.0.1:8765', args)
        self.assertEqual(args[args.index('-S') + 1], str(self.p.ssh))
        value['browser'] = True
        self.assertIn('127.0.0.1:9222:127.0.0.1:9222', self.c.master_command(value))

    def test_prepare_sends_clipboard_token_on_stdin_not_command_line(self):
        value = self.connection(clipboard_token='secret-token')
        with patch.object(app.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.c.prepare(value, False)
        args, kwargs = run.call_args
        self.assertNotIn('secret-token', ' '.join(args[0]))
        request = json.loads(kwargs['input'])
        self.assertEqual(request['token'], 'secret-token')
        self.assertEqual(request['xclip'], app.clipboard.SHIM)

    def test_channels_cannot_fall_back_to_another_connection(self):
        args = self.c.channel_command(self.connection(), 'remote command', tty=True)
        self.assertIn('-t', args)
        self.assertIn('ProxyCommand=false', args)
        self.assertIn('BatchMode=yes', args)
        self.assertIn('ControlMaster=no', args)
        self.assertNotIn('-M', args)
        self.assertNotIn('-R', args)
        self.assertEqual(args[-2:], ['user@host', 'remote command'])

    def test_remote_quoting_preserves_shell_metacharacters(self):
        path = '/tmp/repo space;$(touch SHOULD_NOT_EXIST)'
        args = shlex.split(app.remote_command('open', [path], False))
        self.assertEqual(args[:2], ['python3', '-c'])
        self.assertEqual(json.loads(args[3])['paths'], [path])

    def test_busy_port_is_preserved(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with self.assertRaises(RuntimeError):
                app.require_free_port(listener.getsockname()[1])
            self.assertGreater(listener.fileno(), 0)

    def test_commented_configuration_reports_required_format_before_start(self):
        self.c.config_dir.mkdir(parents=True)
        for invalid in ['{\n // comment\n "workspaces": {}}', '{"workspaces": {},}']:
            with self.subTest(config=invalid):
                self.c.config_file.write_text(invalid)
                with patch.object(self.c, 'service_active', return_value=False), \
                        patch.object(self.c, 'systemctl', side_effect=AssertionError('service changed')):
                    with self.assertRaisesRegex(ValueError, 'strict JSON.*comments.*trailing commas'):
                        self.c.start('tester@server', 22, False)
                self.assertEqual(self.c.config_file.read_text(), invalid)
                self.assertEqual(self.c.connections(), [])

    def test_status_read_tolerates_concurrent_connection_removal(self):
        self.connection()
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
            self.assertIsNone(self.c.read_connection(self.key))

    def test_open_uses_current_directory_and_keeps_connection(self):
        self.connection()
        command = []
        def channel(value, remote, tty=True):
            command.append(json.loads(shlex.split(remote)[3]))
            return ['/bin/true']
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', side_effect=channel), \
                patch.object(Path, 'cwd', return_value=self.home), \
                patch.object(self.c, 'cleanup', side_effect=AssertionError('open closed the shared connection')):
            self.assertEqual(self.c.open([]), 0)
        self.assertEqual(command[0]['paths'], [str(self.home)])
        self.assertEqual(self.c.read_connection(self.key)['id'], 'bridge')
        self.assertEqual(list(self.c.sessions_dir.glob('*.json')), [])

    class Tty(io.StringIO):
        def isatty(self):
            return True

    def open_with_exit(self, command, out):
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/sh', '-c', command]), \
                patch.object(app.sys, 'stdout', out):
            return self.c.open([str(self.home)])

    def test_open_always_restores_terminal_modes_after_the_channel(self):
        self.connection()
        for command, code in [('exit 0', 0), ('exit 1', 1), ('exit 255', 255), ('kill -9 $$', -9)]:
            out = self.Tty()
            self.assertEqual(self.open_with_exit(command, out), code)
            written = out.getvalue()
            self.assertTrue(written.endswith(app.TERMINAL_MODES), command)
            self.assertIn(app.KEYBOARD_RESET, written)
            # Only an unclean end may have left the alternate screen; a clean exit keeps the cursor position.
            self.assertEqual(app.ALTERNATE_SCREEN_EXIT in written, code != 0, command)
            if code:
                self.assertLess(written.index(app.ALTERNATE_SCREEN_EXIT), written.index(app.KEYBOARD_RESET),
                                'leave the alternate screen before popping the main-screen keyboard stack')

    def test_open_restores_terminal_when_interrupted(self):
        self.connection()
        out = self.Tty()
        real_wait = subprocess.Popen.wait
        def wait(process, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt  # SIGTERM/SIGHUP are mapped to this while Claude runs
            return real_wait(process, timeout)
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/sleep', '30']), \
                patch.object(app.sys, 'stdout', out), \
                patch.object(subprocess.Popen, 'wait', wait):
            with self.assertRaises(KeyboardInterrupt):
                self.c.open([str(self.home)])
        self.assertIn(app.ALTERNATE_SCREEN_EXIT, out.getvalue())
        self.assertTrue(out.getvalue().endswith(app.TERMINAL_MODES))

    def test_open_writes_no_escape_codes_without_a_terminal(self):
        self.connection()
        out = io.StringIO()
        self.assertEqual(self.open_with_exit('exit 255', out), 255)
        self.assertNotIn('\033', out.getvalue())

    def test_terminal_reset_covers_common_tui_modes(self):
        for mode in ['1', '25h', '1000', '1002', '1003', '1004', '1005', '1006', '1015', '2004', '2026']:
            self.assertIn('\033[?' + mode, app.TERMINAL_MODES)
        self.assertNotIn('1049', app.TERMINAL_MODES)

    def test_open_outside_scope_does_not_restart_or_expand_service(self):
        self.connection(scope=[str(self.home / 'inside')])
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'systemctl', side_effect=AssertionError('service restarted')):
            with self.assertRaises(ValueError):
                self.c.open([str(self.home)])
        self.assertEqual(self.c.read_connection(self.key)['scope'], [str(self.home / 'inside')])

    def test_open_exit_removes_only_its_own_session(self):
        self.connection()
        other = self.child()
        self.session('other', other)
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/true']):
            self.c.open([str(self.home)])
        self.assertIsNone(other.poll())
        self.assertTrue((self.c.sessions_dir / 'other.json').exists())
        self.assertEqual(self.c.read_connection(self.key)['id'], 'bridge')

    def test_stop_closes_all_owned_channels_but_not_stale_pid(self):
        self.connection()
        children = [self.child(), self.child(), self.child()]
        for index, process in enumerate(children):
            record = app.process_record(process.pid)
            if index == 2:
                record['start'] = 'stale'
            app.atomic_json(self.c.sessions_dir / ('%s.json' % index), {
                'id': str(index), 'client': 'claude', 'connection': self.key, 'paths': [], 'ssh': record})
        with patch.object(self.c, 'control', return_value=subprocess.CompletedProcess([], 1, '', '')):
            self.c.cleanup(self.key, 'bridge')
        self.assertIsNotNone(children[0].poll())
        self.assertIsNotNone(children[1].poll())
        self.assertIsNone(children[2].poll())
        self.assertFalse(self.p.connection.exists())

    def test_failed_master_start_rolls_back_owned_service(self):
        self.c.config_dir.mkdir(parents=True)
        self.c.config_file.write_text(json.dumps({'workspaces': {'allowedRoots': [str(self.home)]}}))
        marker = self.home / 'service-alive'
        def systemctl(*args, **kwargs):
            if args[0] == 'start': marker.touch()
            if args[0] == 'stop': marker.unlink(missing_ok=True)
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(self.c, 'systemctl', side_effect=systemctl), \
                patch.object(self.c, 'service_active', return_value=False), \
                patch.object(self.c, 'wait_health'), \
                patch.object(self.c, 'master_command', return_value=['/bin/false']), \
                patch.object(app, 'require_free_port'):
            with self.assertRaises(RuntimeError):
                self.c.start('user@host', 22, False)
        self.assertFalse(marker.exists())
        self.assertFalse(self.p.connection.exists())

    def test_cleanup_preserves_borrowed_browser(self):
        marker = self.home / 'browser-alive'
        marker.touch()
        self.connection(browser=True, browser_owned=False)
        with patch.object(self.c, 'control'), \
                patch.object(self.c.browser, 'stop', side_effect=lambda *args: marker.unlink()):
            self.c.cleanup(self.key, 'bridge')
        self.assertTrue(marker.exists())

    def test_start_signal_during_recording_recovers_transport(self):
        self.c.config_dir.mkdir(parents=True)
        self.c.config_file.write_text(json.dumps({'workspaces': {'allowedRoots': [str(self.home)]}}))
        real_record, pids = app.process_record, []
        def record(pid):
            if pid != os.getpid() and not pids:
                pids.append(pid)
                os.kill(os.getpid(), signal.SIGINT)
            return real_record(pid)
        with patch.object(self.c, 'systemctl'), \
                patch.object(self.c, 'service_active', return_value=False), \
                patch.object(self.c, 'wait_health'), \
                patch.object(self.c, 'master_command', return_value=['/bin/sleep', '30']), \
                patch.object(app, 'require_free_port'), \
                patch.object(app, 'process_record', side_effect=record):
            with self.assertRaises(KeyboardInterrupt):
                self.c.start('user@host', 22, False)
        self.assertIsNone(real_record(pids[0]))
        self.assertFalse(self.p.connection.exists())

    def test_open_signal_recovers_channel_and_preserves_connection(self):
        self.connection()
        real_record, pids = app.process_record, []
        def record(pid):
            if not pids:
                pids.append(pid)
                os.kill(os.getpid(), signal.SIGINT)
            return real_record(pid)
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/sleep', '30']), \
                patch.object(app, 'process_record', side_effect=record):
            with self.assertRaises(KeyboardInterrupt):
                self.c.open([str(self.home)])
        self.assertIsNone(real_record(pids[0]))
        self.assertTrue(self.p.connection.exists())
        self.assertFalse(list(self.c.sessions_dir.glob('*.json')))

    def test_watcher_recovers_all_channels_after_master_loss(self):
        self.connection()
        child = self.child()
        self.session('one', child)
        with patch.object(self.c, 'connection_ready', return_value=False):
            self.c.watch(self.key, 'bridge')
        self.assertIsNotNone(child.poll())
        self.assertFalse(self.p.connection.exists())

    def test_status_lists_each_live_and_stale_session(self):
        self.connection()
        for name, live in [('one', True), ('two', True), ('stale', False)]:
            record = app.process_record(self.child().pid)
            if not live:
                record['start'] = 'stale'
            app.atomic_json(self.c.sessions_dir / (name + '.json'), {
                'id': name, 'client': 'claude', 'connection': self.key, 'paths': [str(self.home)], 'ssh': record})
        with patch.object(self.c, 'service_active', return_value=True), \
                patch.object(self.c, 'control', return_value=subprocess.CompletedProcess([], 0, '', '')):
            result = self.c.status()
        self.assertTrue(result['running'])
        self.assertEqual(result['active_sessions'], 2)
        self.assertEqual(len(result['connections'][0]['sessions']), 3)

    def test_repeated_start_reuses_target_and_rejects_changes(self):
        self.connection()
        key = self.home / 'other-key'
        key.touch()
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'master_command', side_effect=AssertionError('second master')):
            self.c.start('user@host', 22, False)
            self.c.start(None, None, False)
            with self.assertRaisesRegex(RuntimeError, 'recorded'):
                self.c.start('user@host', 22, False, str(key))
        self.assertEqual(self.c.read_connection(self.key)['host'], 'user@host')

    def browser_environment(self, *, browser=False, fail_forward=False, timeout_forward=False, interrupt=False):
        self.connection(browser=browser)
        child = self.child()
        self.session('existing', child)
        browser_alive = self.home / 'browser-alive'
        forwarding = self.home / 'browser-forward'
        if browser:
            forwarding.touch()
        def browser_start():
            created = not browser_alive.exists()
            browser_alive.touch()
            if interrupt:
                os.kill(os.getpid(), signal.SIGINT)
            return {'created_browser': created, 'session_stamp': 'browser-stamp', 'chrome_running': True}
        def browser_stop(expected_stamp=None):
            self.assertEqual(expected_stamp, 'browser-stamp')
            browser_alive.unlink(missing_ok=True)
        def run(args, **kwargs):
            if '-O' not in args:
                self.assertIn('-T', args)
                payload = json.loads(shlex.split(args[-1])[3])
                self.assertEqual(payload['action'], 'prepare')
                self.assertTrue(payload['browser'])
                return subprocess.CompletedProcess(args, 0, '', '')
            action = args[args.index('-O') + 1]
            if action in ['forward', 'cancel']:
                self.assertIn('-R', args)
                self.assertIn('127.0.0.1:9222:127.0.0.1:9222', args)
                if action == 'forward':
                    if fail_forward:
                        return subprocess.CompletedProcess(args, 1, '', 'remote port occupied')
                    if forwarding.exists():
                        raise AssertionError('duplicate forwarding request')
                    forwarding.touch()
                    if timeout_forward:
                        raise subprocess.TimeoutExpired(args, 5)
                else:
                    forwarding.unlink(missing_ok=True)
            else:
                self.assertEqual(action, 'check')
            return subprocess.CompletedProcess(args, 0, '', '')
        for manager in [
                patch.object(self.c, 'service_active', return_value=True),
                patch.object(self.c, 'systemctl', side_effect=AssertionError('service changed')),
                patch.object(self.c.browser, 'start', side_effect=browser_start),
                patch.object(self.c.browser, 'stop', side_effect=browser_stop),
                patch.object(app.subprocess, 'run', side_effect=run)]:
            manager.start()
            self.addCleanup(manager.stop)
        return child, browser_alive, forwarding

    def test_start_enables_browser_without_restarting_existing_sessions(self):
        child, browser_alive, forwarding = self.browser_environment()
        self.c.start('user@host', 22, True)
        value = self.c.read_connection(self.key)
        self.assertTrue(value['browser'])
        self.assertTrue(self.c.read_shared()['browser_owned'])
        self.assertEqual(value['id'], 'bridge')
        self.assertTrue(browser_alive.exists())
        self.assertTrue(forwarding.exists())
        self.assertIsNone(child.poll())
        self.assertTrue((self.c.sessions_dir / 'existing.json').is_file())
        self.c.start('user@host', 22, True)
        self.assertTrue(self.c.read_shared()['browser_owned'])

    def test_start_reopens_closed_browser_without_adding_duplicate_forward(self):
        child, browser_alive, forwarding = self.browser_environment(browser=True)
        self.c.start('user@host', 22, True)
        self.assertTrue(browser_alive.exists())
        self.assertTrue(forwarding.exists())
        self.assertTrue(self.c.read_shared()['browser_owned'])
        self.assertIsNone(child.poll())

    def test_browser_forward_failure_preserves_connection_and_sessions(self):
        child, browser_alive, forwarding = self.browser_environment(fail_forward=True)
        before = self.c.read_connection(self.key)
        with self.assertRaisesRegex(RuntimeError, 'remote port occupied'):
            self.c.start('user@host', 22, True)
        self.assertEqual(self.c.read_connection(self.key), before)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_browser_record_failure_cancels_added_forward(self):
        child, browser_alive, forwarding = self.browser_environment()
        before = self.c.read_connection(self.key)
        original = self.c.write_connection
        def write(value):
            if value['browser']:
                raise OSError('disk full')
            return original(value)
        with patch.object(self.c, 'write_connection', side_effect=write):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.c.start('user@host', 22, True)
        self.assertEqual(self.c.read_connection(self.key), before)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_browser_forward_timeout_cancels_uncertain_forward(self):
        child, browser_alive, forwarding = self.browser_environment(timeout_forward=True)
        before = self.c.read_connection(self.key)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.c.start('user@host', 22, True)
        self.assertFalse(forwarding.exists())
        self.assertFalse(browser_alive.exists())
        self.assertEqual(self.c.read_connection(self.key), before)
        self.assertIsNone(child.poll())

    def test_interrupted_browser_upgrade_rolls_back_only_new_resources(self):
        child, browser_alive, forwarding = self.browser_environment(interrupt=True)
        before = self.c.read_connection(self.key)
        with self.assertRaises(KeyboardInterrupt):
            self.c.start('user@host', 22, True)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertEqual(self.c.read_connection(self.key), before)
        self.assertIsNone(child.poll())

    def test_browser_prepare_failure_does_not_create_local_resources(self):
        child, browser_alive, forwarding = self.browser_environment()
        before = self.c.read_connection(self.key)
        self.c.write_connection(dict(before, name='box'))
        before = self.c.read_connection(self.key)
        with patch.object(app.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CompletedProcess([], 1, '', '')]):
            with self.assertRaises(RuntimeError) as raised:
                self.c.start('user@host', 22, True)
        self.assertEqual(str(raised.exception), 'Remote preparation of box (user@host:22) failed; '
                         'if it lacks remote-dev, run: remote-dev install -H box --browser')
        self.assertEqual(self.c.read_connection(self.key), before)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_failed_browser_upgrade_preserves_borrowed_chrome(self):
        child, browser_alive, forwarding = self.browser_environment(fail_forward=True)
        browser_alive.touch()
        with self.assertRaisesRegex(RuntimeError, 'remote port occupied'):
            self.c.start('user@host', 22, True)
        self.assertTrue(browser_alive.exists())
        self.assertFalse(self.c.read_connection(self.key)['browser'])
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_stop_holds_lifecycle_lock_until_watcher_is_stopped(self):
        self.connection(watcher_owned=True)
        interleaved = self.home / 'new-start-interleaved'
        def watcher_stop(*args, **kwargs):
            if kwargs.get('unit') == self.p.unit:
                try:
                    with self.c.lock(self.p.lock, nonblocking=True):
                        interleaved.touch()
                except RuntimeError:
                    pass
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(self.c, 'systemctl', side_effect=watcher_stop):
            self.c.stop()
        self.assertFalse(interleaved.exists(), 'A new start entered before old watcher cleanup')

    def test_stop_removes_abandoned_unix_control_socket(self):
        self.c.runtime.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.c.paths(app.LEGACY).ssh))
        with patch.object(self.c, 'service_active', return_value=False):
            self.c.stop()
        self.assertFalse(self.c.paths(app.LEGACY).ssh.exists())

    def test_stop_preserves_untracked_active_control_socket(self):
        self.c.runtime.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.c.paths(app.LEGACY).ssh))
            listener.listen()
            with patch.object(self.c, 'service_active', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'Untracked.*socket'):
                    self.c.stop()
            self.assertTrue(self.c.paths(app.LEGACY).ssh.exists())

    def test_stop_preserves_unexpected_file_at_control_socket_path(self):
        self.c.runtime.mkdir(parents=True)
        self.c.paths(app.LEGACY).ssh.write_text('user-owned file')
        with patch.object(self.c, 'service_active', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'socket'):
                self.c.stop()
        self.assertEqual(self.c.paths(app.LEGACY).ssh.read_text(), 'user-owned file')

    def test_session_record_write_failure_reaps_channel_not_connection(self):
        self.connection()
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/sleep', '30']), \
                patch.object(app, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.c.open([str(self.home)])
        self.assertTrue(self.p.connection.exists())
        self.assertFalse(list(self.c.sessions_dir.glob('*.json')))

    # Multiple connections, target resolution, OAuth callback and legacy state.

    def ok(self, *args, **kwargs):
        return subprocess.CompletedProcess([], 0, '', '')

    def two_connections(self, **b):
        a = self.connection()
        b = self.connection(id='other', host='me@nexcom23', **b)
        return a, b

    def test_hostname_prefix_matching(self):
        items = [{'host': 'user@nexcom23', 'ssh_port': 22}, {'host': 'ubuntu@aws-sg', 'ssh_port': 22},
                 {'host': 'root@nexcom24', 'ssh_port': 2222}, {'host': 'dev-server', 'ssh_port': 22}]
        self.assertEqual(app.match_targets('aws', items), [items[1]])
        self.assertEqual(app.match_targets('dev', items), [items[3]])
        self.assertEqual(len(app.match_targets('nexcom', items)), 2)
        self.assertEqual(app.match_targets('root@nex', items), [items[2]])
        self.assertEqual(app.match_targets('ubuntu@aws-sg', items), [items[1]])
        self.assertEqual(app.match_targets('23', items), [], 'only hostname prefixes match')
        self.assertEqual(app.match_targets('sg', items), [])

    def remembered(self, *targets):
        app.atomic_json(self.c.targets_file, {'targets': [
            {'host': host, 'ssh_port': port, 'identity_file': identity, 'last_used': used}
            for host, port, identity, used in targets]})

    def test_start_resolves_remembered_targets(self):
        with self.assertRaisesRegex(RuntimeError, 'pass --host'):
            self.c.resolve_start(None, None, None)
        self.remembered(('user@nexcom23', 2222, '/keys/nex', 10), ('ubuntu@aws-sg', 22, '/keys/aws', 20),
                        ('ubuntu@aws-tokyo', 22, None, 5))
        last = self.c.resolve_start(None, None, None)
        self.assertEqual((last['host'], last['identity_file'], last['note']), ('ubuntu@aws-sg', '/keys/aws', 'last used'))
        nex = self.c.resolve_start('nex', None, None)
        self.assertEqual((nex['host'], nex['ssh_port'], nex['identity_file']), ('user@nexcom23', 2222, '/keys/nex'))
        self.assertIn('matched', nex['note'])
        override = self.c.resolve_start('nex', None, '/keys/other')
        self.assertEqual(override['identity_file'], '/keys/other')
        with self.assertRaisesRegex(RuntimeError, 'several'):
            self.c.resolve_start('aws', None, None)
        self.assertEqual(self.c.resolve_start('aws', 2200, None)['host'], 'aws', 'port filters candidates')
        literal = self.c.resolve_start('new@box', None, None)
        self.assertEqual((literal['host'], literal['ssh_port'], literal['identity_file']), ('new@box', 22, None))
        with self.assertRaises(ValueError):
            self.c.resolve_start('-oProxyCommand=x', None, None)

    def test_start_without_host_follows_running_connections(self):
        self.remembered(('ubuntu@aws-sg', 22, None, 20))
        self.connection()
        self.assertEqual(self.c.resolve_start(None, None, None)['host'], 'user@host')
        self.connection(id='other', host='me@nexcom23')
        with self.assertRaisesRegex(RuntimeError, 'Several connections'):
            self.c.resolve_start(None, None, None)
        self.assertEqual(self.c.resolve_start('nex', None, None)['host'], 'me@nexcom23')

    def test_remember_upserts_most_recent_target(self):
        self.remembered(('user@host', 22, None, 1), ('me@nexcom23', 22, None, 2))
        self.c.remember('user@host', 22, '/k')
        targets = self.c.read_targets()
        self.assertEqual([(t['host'], t['identity_file']) for t in targets], [('user@host', '/k'), ('me@nexcom23', None)])
        self.assertEqual(self.c.targets_file.stat().st_mode & 0o777, 0o600)

    def test_active_resolution_uses_single_connection_or_explicit_choice(self):
        with self.assertRaisesRegex(RuntimeError, 'start'):
            self.c.resolve_active()
        self.connection()
        self.assertEqual(self.c.resolve_active()['host'], 'user@host')
        self.connection(id='other', host='me@nexcom23')
        with self.assertRaisesRegex(RuntimeError, 'Several connections.*REMOTE_DEV_HOST'):
            self.c.resolve_active()
        self.assertEqual(self.c.resolve_active('nex')['host'], 'me@nexcom23')
        with patch.dict(os.environ, {'REMOTE_DEV_HOST': 'host'}):
            self.assertEqual(self.c.resolve_active()['host'], 'user@host')
            self.assertEqual(self.c.resolve_active('nex')['host'], 'me@nexcom23')
        with patch.dict(os.environ, {'REMOTE_DEV_HOST': 'aws'}):
            with self.assertRaisesRegex(RuntimeError, 'No running connection matches aws'):
                self.c.resolve_active()

    def test_open_records_connection_and_uses_selected_master(self):
        a, b = self.two_connections()
        seen = []
        def channel(value, remote, tty=True):
            seen.append(value['key'])
            for _, record in self.c.sessions():
                seen.append(record['connection'])
            return ['/bin/true']
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', side_effect=channel):
            with self.assertRaisesRegex(RuntimeError, 'Several'):
                self.c.open([str(self.home)])
            self.assertEqual(self.c.open([str(self.home)], host='nex'), 0)
        self.assertEqual(seen[0], b['key'])
        self.assertEqual(self.c.channel_command(b, 'x')[3], str(self.c.paths(b['key']).ssh))

    def test_cleanup_of_one_connection_keeps_other_sessions_and_shared_resources(self):
        a, b = self.two_connections(browser=True)
        mine, theirs = self.child(), self.child()
        self.session('mine', mine)
        self.session('theirs', theirs, key=b['key'])
        self.c.write_shared({'devspace_owned': True, 'browser_owned': True, 'browser_stamp': 's',
                             'oauth_holder': self.key})
        with patch.object(self.c, 'control', side_effect=self.ok), \
                patch.object(self.c, 'systemctl', side_effect=AssertionError('shared DevSpace stopped')), \
                patch.object(self.c.browser, 'stop', side_effect=AssertionError('shared Chrome stopped')):
            self.c.cleanup(self.key, 'bridge')
        self.assertIsNotNone(mine.poll())
        self.assertIsNone(theirs.poll())
        self.assertIsNone(self.c.read_connection(self.key))
        self.assertFalse(self.p.dir.exists())
        self.assertEqual(self.c.read_shared(), {'devspace_owned': True, 'browser_owned': True, 'browser_stamp': 's'})

    def test_last_cleanup_stops_owned_shared_resources(self):
        a, b = self.two_connections()
        self.connection(browser=True)
        self.c.write_shared({'devspace_owned': True, 'browser_owned': True, 'browser_stamp': 's'})
        stopped = []
        with patch.object(self.c, 'control', side_effect=self.ok), \
                patch.object(self.c, 'systemctl', side_effect=lambda *a, **k: stopped.append(a)), \
                patch.object(self.c.browser, 'stop', side_effect=lambda expected_stamp: stopped.append(expected_stamp)):
            self.c.cleanup(self.key, 'bridge')
            self.assertEqual(stopped, ['s'], 'Chrome stops with its last browser connection')
            self.c.cleanup(b['key'], 'other')
        self.assertEqual(stopped, ['s', ('stop',)])
        self.assertFalse(self.c.shared_file.exists())

    def test_failed_service_stop_is_retried_by_stop(self):
        self.connection()
        self.c.write_shared({'devspace_owned': True})
        with patch.object(self.c, 'control', side_effect=self.ok), \
                patch.object(self.c, 'systemctl', side_effect=subprocess.CalledProcessError(1, 'systemctl')):
            with self.assertRaisesRegex(RuntimeError, 'retry stop'):
                self.c.cleanup(self.key, 'bridge')
        self.assertEqual(self.c.read_shared(), {'devspace_owned': True})
        calls = []
        with patch.object(self.c, 'systemctl', side_effect=lambda *a, **k: calls.append(a)), \
                patch.object(self.c, 'service_active', return_value=True):
            self.c.stop()
        self.assertEqual(calls, [('stop',)])
        self.assertEqual(self.c.read_shared(), {})

    def start_fixture(self):
        self.c.config_dir.mkdir(parents=True)
        self.c.config_file.write_text(json.dumps({'workspaces': {'allowedRoots': [str(self.home)]}}))

    def test_second_connection_reuses_running_devspace_and_rolls_back_alone(self):
        self.start_fixture()
        self.connection(host='me@nexcom23')
        self.c.write_shared({'devspace_owned': True})
        with patch.object(self.c, 'systemctl', side_effect=AssertionError('service changed')), \
                patch.object(self.c, 'service_active', return_value=True), \
                patch.object(self.c, 'wait_health'), \
                patch.object(self.c, 'master_command', return_value=['/bin/false']), \
                patch.object(app, 'require_free_port', side_effect=AssertionError('port probed')):
            with self.assertRaisesRegex(RuntimeError, 'another name or user'):
                self.c.start('user@host', 22, False)
        self.assertIsNone(self.c.read_connection(self.key))
        self.assertEqual([item['host'] for item in self.c.connections()], ['me@nexcom23'])
        self.assertEqual(self.c.read_shared(), {'devspace_owned': True})

    def test_untracked_service_without_connections_is_refused(self):
        self.start_fixture()
        with patch.object(self.c, 'service_active', return_value=True), \
                patch.object(self.c, 'systemctl', side_effect=AssertionError('service changed')):
            with self.assertRaisesRegex(RuntimeError, 'Untracked'):
                self.c.start('user@host', 22, False)

    def test_successful_start_leaves_oauth_to_open_and_remembers_target(self):
        self.start_fixture()
        actions, master = [], self.child()
        def control(values, action, *args):
            actions.append((values['host'], action) + args)
            return subprocess.CompletedProcess([], 0, 'Master running (pid=%d)' % master.pid, '')
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(self.c, 'systemctl', side_effect=self.ok), \
                patch.object(self.c, 'service_active', return_value=False), \
                patch.object(self.c, 'wait_health'), \
                patch.object(self.c, 'master_command', return_value=['/bin/true']), \
                patch.object(self.c, 'control', side_effect=control), \
                patch.object(app, 'require_free_port'), \
                patch.object(app.subprocess, 'run', side_effect=run):
            self.c.start('user@host', 22, False)
        self.assertNotIn(('user@host', 'forward') + app.OAUTH_FORWARD, actions)
        self.assertEqual(self.c.read_shared(), {'devspace_owned': True})
        value = self.c.read_connection(self.key)
        self.assertEqual((value['phase'], value['watcher_owned']), ('ready', True))
        self.assertEqual(self.c.read_targets()[0]['host'], 'user@host')

    def test_open_moves_oauth_callback_to_its_connection(self):
        self.move_patch.stop()
        a, b = self.two_connections()
        self.c.write_shared({'oauth_holder': self.key})
        actions = []
        def control(values, action, *args):
            actions.append((values['host'], action))
            return subprocess.CompletedProcess([], 0, '', '')
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/true']), \
                patch.object(self.c, 'control', side_effect=control), \
                patch.object(app.sys, 'stdout', io.StringIO()) as out:
            self.assertEqual(self.c.open([str(self.home)], host='nex'), 0)
            self.assertEqual(actions, [('user@host', 'cancel'), ('me@nexcom23', 'forward')])
            self.assertIn('OAuth callback 8765 now forwards to me@nexcom23:22', out.getvalue())
            self.c.open([str(self.home)], host='nex')
        self.assertEqual(len(actions), 2, 'the holder keeps the callback without ssh calls')
        self.assertEqual(self.c.read_shared(), {'oauth_holder': b['key']})

    def test_open_runs_even_if_the_callback_cannot_move(self):
        self.move_patch.stop()
        self.connection()
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/true']), \
                patch.object(self.c, 'control', return_value=subprocess.CompletedProcess([], 1, '', 'in use')), \
                patch.object(app.sys, 'stderr', io.StringIO()) as err:
            self.assertEqual(self.c.open([str(self.home)]), 0)
        self.assertIn('8765', err.getvalue())
        self.assertEqual(self.c.read_shared(), {})

    def test_auth_moves_oauth_forward(self):
        self.move_patch.stop()
        a, b = self.two_connections()
        self.c.write_shared({'oauth_holder': self.key})
        actions = []
        def control(values, action, *args):
            actions.append((values['host'], action))
            return subprocess.CompletedProcess([], 0, '', '')
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'control', side_effect=control):
            self.c.auth('nex')
        self.assertEqual(actions, [('user@host', 'cancel'), ('me@nexcom23', 'forward')])
        self.assertEqual(self.c.read_shared(), {'oauth_holder': b['key']})

    def test_failed_auth_restores_previous_holder(self):
        self.move_patch.stop()
        a, b = self.two_connections()
        self.c.write_shared({'oauth_holder': self.key})
        def control(values, action, *args):
            return subprocess.CompletedProcess([], int(values['host'] == 'me@nexcom23'), '', 'busy')
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'control', side_effect=control):
            with self.assertRaisesRegex(RuntimeError, '8765'):
                self.c.auth('nex')
        self.assertEqual(self.c.read_shared(), {'oauth_holder': self.key})

    def test_stop_requires_choice_with_several_connections(self):
        a, b = self.two_connections()
        with patch.object(self.c, 'control', side_effect=self.ok), \
                patch.object(self.c, 'systemctl', side_effect=self.ok), \
                patch.object(self.c, 'service_active', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'Several'):
                self.c.stop()
            self.c.stop('nex')
            self.assertEqual([item['host'] for item in self.c.connections()], ['user@host'])
            self.connection(id='other', host='me@nexcom23')
            self.c.stop(everything=True)
        self.assertEqual(self.c.connections(), [])

    def test_status_groups_sessions_by_connection(self):
        a, b = self.two_connections()
        self.session('mine', self.child())
        self.session('theirs', self.child(), key=b['key'])
        self.session('old', self.child(), connection=None)
        self.c.write_shared({'oauth_holder': b['key']})
        with patch.object(self.c, 'service_active', return_value=True), \
                patch.object(self.c, 'control', side_effect=self.ok):
            result = self.c.status()
            single = self.c.status('nex')
        self.assertEqual([(c['host'], c['active_sessions'], c['oauth_callback']) for c in result['connections']],
                         [('me@nexcom23', 1, True), ('user@host', 1, False)])
        self.assertEqual(result['active_sessions'], 2)
        self.assertEqual([c['host'] for c in single['connections']], ['me@nexcom23'])

    def legacy(self, **overrides):
        value = {'id': 'old', 'phase': 'ready', 'host': 'user@old', 'ssh_port': 22, 'scope': [str(self.home)],
                 'browser': False, 'service_owned': True, 'browser_owned': False, 'watcher_owned': True}
        value.update(overrides)
        app.atomic_json(self.c.runtime / 'connection.json', value)
        return self.c.read_connection(app.LEGACY)

    def test_legacy_connection_is_listed_used_and_stopped(self):
        legacy = self.legacy(browser=True, browser_owned=True, browser_stamp='old-stamp')
        self.assertEqual([item['key'] for item in self.c.connections()], [app.LEGACY])
        self.assertEqual(self.c.channel_command(legacy, 'x')[3], str(self.c.runtime / 'ssh.sock'))
        child = self.child()
        app.atomic_json(self.c.sessions_dir / 'old.json', {
            'id': 'old', 'client': 'claude', 'paths': [], 'ssh': app.process_record(child.pid)})
        calls = []
        with patch.object(self.c, 'control', side_effect=self.ok), \
                patch.object(self.c, 'systemctl', side_effect=lambda *a, **k: calls.append((a, k.get('unit')))), \
                patch.object(self.c.browser, 'stop', side_effect=lambda expected_stamp: calls.append(expected_stamp)):
            self.c.stop()
        self.assertIsNotNone(child.poll())
        self.assertIn((('stop',), None), calls)
        self.assertIn('old-stamp', calls)
        self.assertIn((('stop',), app.WATCH_UNIT), calls)
        self.assertFalse((self.c.runtime / 'connection.json').exists())
        self.assertEqual(self.c.read_shared(), {})

    def test_new_connection_is_refused_while_legacy_runs(self):
        self.start_fixture()
        self.legacy()
        with patch.object(self.c, 'service_active', return_value=True), \
                patch.object(self.c, 'systemctl', side_effect=AssertionError('service changed')):
            with self.assertRaisesRegex(RuntimeError, 'older remote-dev'):
                self.c.start('user@host', 22, False)

    def test_watch_argument_forms(self):
        calls = []
        with patch.object(app.Controller, 'watch', side_effect=lambda *args: calls.append(args)):
            app.main(['_watch', 'abc123', 'id-1'])
            app.main(['_watch', 'id-2'])
        self.assertEqual(calls, [('abc123', 'id-1'), (app.LEGACY, 'id-2')])

    def test_install_refuses_while_any_connection_runs(self):
        self.connection(host='me@nexcom23')
        with patch.object(self.c, 'service_active', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'Stop shared access'):
                self.c.install()

    def test_prefix_does_not_continue_digits_or_match_empty_hostnames(self):
        items = [{'host': 'me@10.0.0.12', 'ssh_port': 22}, {'host': 'server10', 'ssh_port': 22},
                 {'host': 'deploy@prod-db', 'ssh_port': 22}]
        self.assertEqual(app.match_targets('10.0.0.1', items), [])
        self.assertEqual(app.match_targets('server1', items), [])
        self.assertEqual(app.match_targets('me@', items), [])
        self.assertEqual(app.match_targets('server', items), [items[1]])
        self.assertEqual(app.match_targets('prod', items), [items[2]])
        self.assertEqual(app.match_targets('=prod', items), [], '= disables prefix matching')

    def test_start_uses_new_literal_targets_instead_of_longer_remembered_names(self):
        self.remembered(('me@10.0.0.12', 22, '/k', 1), ('server10', 22, None, 2), ('deploy@prod-db', 22, None, 3))
        self.assertEqual(self.c.resolve_start('10.0.0.1', None, None)['host'], '10.0.0.1')
        self.assertEqual(self.c.resolve_start('server1', None, None)['host'], 'server1')
        self.assertEqual(self.c.resolve_start('prod', None, None)['host'], 'deploy@prod-db')
        literal = self.c.resolve_start('=prod', None, None)
        self.assertEqual((literal['host'], literal['identity_file'], literal['note']), ('prod', None, None))
        self.assertEqual(self.c.resolve_start('=server10', None, None)['host'], 'server10')

    def test_host_port_selects_between_connections_on_one_host(self):
        self.connection(host='me@gw', ssh_port=2201)
        self.connection(id='other', host='me@gw', ssh_port=2202)
        with self.assertRaisesRegex(RuntimeError, 'HOST:PORT'):
            self.c.resolve_active('gw')
        self.assertEqual(self.c.resolve_active('gw:2202')['ssh_port'], 2202)
        self.assertEqual(self.c.resolve_active('me@gw:2201')['ssh_port'], 2201)
        started = self.c.resolve_start('gw:2202', None, None)
        self.assertEqual((started['host'], started['ssh_port']), ('me@gw', 2202))
        self.assertEqual(self.c.resolve_start('new:2022', None, None)['ssh_port'], 2022)
        with self.assertRaisesRegex(ValueError, 'disagree'):
            self.c.resolve_start('gw:2202', 2201, None)
        self.assertEqual(app.parse_query('[::1]:2200'), ('[::1]', 2200, False))
        self.assertEqual(app.parse_query('fe80::1'), ('fe80::1', None, False))

    def test_stop_with_a_named_target_stays_idempotent(self):
        self.c.runtime.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.c.paths(app.LEGACY).ssh))
        with patch.object(self.c, 'service_active', return_value=False), \
                patch.dict(os.environ, {'REMOTE_DEV_HOST': 'srv'}):
            self.c.stop('srv')
            self.c.stop()
        self.assertFalse(self.c.paths(app.LEGACY).ssh.exists())

    def test_start_without_browser_reuses_a_browser_connection(self):
        self.connection(browser=True)
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'enable_browser', side_effect=AssertionError('browser changed')):
            self.c.start(None, None, False)
            self.c.start('host', None, False)
        self.assertTrue(self.c.read_connection(self.key)['browser'])

    def test_unreadable_targets_fail_clearly_and_bad_entries_are_ignored(self):
        self.c.targets_file.parent.mkdir(parents=True)
        self.c.targets_file.write_text('{"targets": [{"ssh_port": 22}, {"host": "ok", "ssh_port": 22}]}')
        self.assertEqual([item['host'] for item in self.c.read_targets()], ['ok'])
        self.c.targets_file.write_text('{broken')
        with self.assertRaisesRegex(RuntimeError, 'targets'):
            self.c.resolve_start(None, None, None)

    def test_failure_to_remember_keeps_a_started_connection(self):
        self.start_fixture()
        master = self.child()
        def control(values, action, *args):
            return subprocess.CompletedProcess([], 0, 'Master running (pid=%d)' % master.pid, '')
        with patch.object(self.c, 'systemctl', side_effect=self.ok), \
                patch.object(self.c, 'service_active', return_value=False), \
                patch.object(self.c, 'wait_health'), \
                patch.object(self.c, 'master_command', return_value=['/bin/true']), \
                patch.object(self.c, 'control', side_effect=control), \
                patch.object(self.c, 'remember', side_effect=OSError('read-only')), \
                patch.object(app, 'require_free_port'), \
                patch.object(app.subprocess, 'run', side_effect=lambda args, **k: subprocess.CompletedProcess(args, 0)), \
                patch.object(app.sys, 'stderr', io.StringIO()) as err:
            self.c.start('user@host', 22, False)
        self.assertIn('not remembered', err.getvalue())
        self.assertEqual(self.c.read_connection(self.key)['phase'], 'ready')

    def test_install_prepares_only_a_new_server_while_connections_run(self):
        self.connection(host='me@nexcom23')
        calls = []
        def remote(*args):
            with self.c.lock(nonblocking=True):  # running connections must not wait for the remote install
                calls.append(args[1])
        with patch.object(self.c, 'service_active', return_value=True), \
                patch.object(app.provisioning, 'local_install', side_effect=AssertionError('local files changed')), \
                patch.object(app.provisioning, 'remote_call', side_effect=remote):
            self.c.install('ubuntu@aws-sg')
        self.assertEqual(calls, ['ubuntu@aws-sg'])

    def test_install_remembers_target_for_start_and_uninstall_forgets_it(self):
        key = self.home / 'aws.pem'
        key.touch()
        calls = []
        def install(app_, root, host, port, identity_file, *args):
            calls.append(('install', host, port, identity_file))
        def uninstall(app_, root, host, port, identity_file):
            calls.append(('uninstall', host, port, identity_file))
        with patch.object(app.provisioning, 'install', side_effect=install), \
                patch.object(app.provisioning, 'uninstall', side_effect=uninstall), \
                patch.object(self.c, 'stop'):
            self.c.install('ubuntu@aws-sg', 2200, str(key))
            target = self.c.resolve_start('aws', None, None)
            self.assertEqual((target['host'], target['ssh_port'], target['identity_file']),
                             ('ubuntu@aws-sg', 2200, str(key)))
            self.c.install('aws', None, None, True)
            self.c.install()
            self.c.uninstall('aws')
        self.assertEqual(calls, [('install', 'ubuntu@aws-sg', 2200, str(key)),
                                 ('install', 'ubuntu@aws-sg', 2200, str(key)),
                                 ('install', None, 22, None),
                                 ('uninstall', 'ubuntu@aws-sg', 2200, str(key))])
        self.assertEqual(self.c.read_targets(), [])

    def test_failed_install_remembers_nothing(self):
        with patch.object(app.provisioning, 'install', side_effect=subprocess.CalledProcessError(1, 'ssh')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.c.install('ubuntu@aws-sg')
        self.assertEqual(self.c.read_targets(), [])

    def test_install_and_uninstall_accept_the_short_host_option(self):
        for command in ['install', 'uninstall']:
            args = app.parser().parse_args([command, '-H', 'aws'])
            self.assertEqual((args.host, args.ssh_port), ('aws', None))

    def test_names_match_exactly_or_by_prefix(self):
        items = [{'host': 'hamlin@172.25.2.23', 'ssh_port': 22, 'name': 'nexcom23'},
                 {'host': 'ubuntu@13.229.1.2', 'ssh_port': 22, 'name': 'aws-sg'},
                 {'host': 'nexus', 'ssh_port': 22}]
        self.assertEqual(app.match_targets('aws', items), [items[1]])
        self.assertEqual(app.match_targets('nexcom23', items), [items[0]])
        self.assertEqual(len(app.match_targets('nex', items)), 2, 'name and hostname prefixes are both candidates')
        self.assertEqual(app.match_targets('172.25', items), [items[0]], 'addresses still work')
        self.assertEqual(app.match_targets('ubuntu@aws', items), [], 'a user restricts to hostnames')
        self.assertEqual(app.match_targets('=aws-sg', items), [items[1]], 'exact name')
        self.assertEqual(app.describe(items[0]), 'nexcom23 (hamlin@172.25.2.23:22)')
        self.assertEqual(app.describe(items[2]), 'nexus:22')

    def test_install_name_is_used_by_start_open_and_status(self):
        calls = []
        with patch.object(app.provisioning, 'install', side_effect=lambda *a: calls.append(a[2:5])):
            self.c.install('hamlin@172.25.2.23', None, None, name='nexcom23')
        self.assertEqual(self.c.read_targets()[0]['name'], 'nexcom23')
        target = self.c.resolve_start('nex', None, None)
        self.assertEqual((target['host'], target['name']), ('hamlin@172.25.2.23', 'nexcom23'))
        self.connection(host='hamlin@172.25.2.23', name='nexcom23')
        self.connection(id='other', host='ubuntu@13.229.1.2')
        self.assertEqual(self.c.resolve_active('nexcom')['host'], 'hamlin@172.25.2.23')
        with patch.object(self.c, 'service_active', return_value=True), \
                patch.object(self.c, 'control', side_effect=self.ok):
            names = [item['name'] for item in self.c.status()['connections']]
        self.assertEqual(names, ['nexcom23', None])

    def test_remember_keeps_existing_name_and_renames_on_request(self):
        self.c.remember('hamlin@172.25.2.23', 22, None, name='nexcom23')
        self.c.remember('hamlin@172.25.2.23', 22, '/k')
        self.assertEqual(self.c.read_targets()[0]['name'], 'nexcom23')
        self.c.remember('hamlin@172.25.2.23', 22, '/k', name='nex1')
        self.assertEqual(self.c.read_targets()[0]['name'], 'nex1')

    def test_invalid_or_duplicate_names_are_rejected(self):
        self.c.remember('ubuntu@13.229.1.2', 22, None, name='aws')
        for name in ['-bad', '=x', 'a b', 'user@x']:
            with self.assertRaises(ValueError):
                self.c.target('hamlin@172.25.2.23', None, None, name)
        with self.assertRaisesRegex(ValueError, 'already names'):
            self.c.target('hamlin@172.25.2.23', None, None, 'aws')
        self.assertEqual(self.c.target('aws', None, None, 'aws')['name'], 'aws')

    def test_start_on_a_running_connection_can_rename_it(self):
        self.connection()
        self.c.remember('user@host', 22, None)
        with patch.object(self.c, 'connection_ready', return_value=True):
            self.c.start('user@host', None, False, name='box')
        self.assertEqual(self.c.read_connection(self.key)['name'], 'box')
        self.assertEqual(self.c.read_targets()[0]['name'], 'box')
        self.assertEqual(self.c.resolve_active('box')['host'], 'user@host')

    def test_start_and_install_accept_a_name(self):
        for command in ['start', 'install']:
            self.assertEqual(app.parser().parse_args([command, '-H', 'h', '--name', 'n']).name, 'n')

    def bootstrap(self, browser=False, path=None, target=None):
        executable = self.home / '.local/bin/claude'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text('#!' + sys.executable + '\nimport pathlib,os,json\n'
            'pathlib.Path(os.environ["HOME"],"launched.json").write_text('
            'json.dumps({"cwd":str(pathlib.Path.cwd()),"path":os.environ.get("PATH")}))\n')
        executable.chmod(0o755)
        config = self.home / '.claude.json'
        if not config.exists():
            config.write_text('{"mcpServers":{"local-dev":{"type":"http","url":"http://127.0.0.1:7676/mcp"}}}')
        paths = ['/tmp/repo space;$(touch SHOULD_NOT_EXIST)']
        payload = {'action': 'open', 'paths': paths, 'browser': browser,
                   'workspace': app.sessions.workspace_name('test-local', paths)}
        if target:
            payload['target'] = target
        environment = dict(os.environ, HOME=str(self.home))
        if path is not None:
            environment['PATH'] = path
        return subprocess.run([sys.executable, '-c', app.REMOTE_BOOTSTRAP, json.dumps(payload)],
            env=environment, capture_output=True, text=True)

    def test_remote_claude_inherits_native_install_directory_on_path(self):
        parent_path = os.environ.get('PATH')
        result = self.bootstrap(path='/usr/bin:/bin')
        self.assertEqual(result.returncode, 0, result.stderr)
        launched = json.loads((self.home / 'launched.json').read_text())
        self.assertEqual(launched['path'], str(self.home / '.local/share/remote-dev/bin') + ':'
                         + str(self.home / '.local/bin') + ':/usr/bin:/bin')
        self.assertEqual(os.environ.get('PATH'), parent_path)

    def test_remote_claude_preserves_path_when_native_directory_already_present(self):
        existing = '/usr/bin:' + str(self.home / '.local/bin') + ':/bin'
        result = self.bootstrap(path=existing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.home / 'launched.json').read_text())['path'],
                         str(self.home / '.local/share/remote-dev/bin') + ':' + existing)

    def test_remote_instructions_name_this_server_for_auth_and_stop(self):
        self.assertEqual(self.bootstrap(target='box').returncode, 0)
        directory = Path(json.loads((self.home / 'launched.json').read_text())['cwd'])
        text = (directory / 'CLAUDE.md').read_text()
        self.assertIn('remote-dev auth -H box', text)
        self.assertIn('remote-dev stop -H box', text)
        self.assertIn('ownerToken', text)
        self.assertEqual(self.bootstrap().returncode, 0, 'older clients send no target')
        self.assertIn('remote-dev auth -H TARGET', (directory / 'CLAUDE.md').read_text())

    def test_open_sends_the_connection_name_as_target(self):
        self.connection(name='box')
        sent = []
        def channel(value, remote, tty=True):
            sent.append(json.loads(shlex.split(remote)[3]))
            return ['/bin/true']
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', side_effect=channel):
            self.c.open([str(self.home)])
        self.assertEqual(sent[0]['target'], 'box')

    def test_same_projects_reuse_remote_directory(self):
        self.assertEqual(self.bootstrap(False).returncode, 0)
        first = json.loads((self.home / 'launched.json').read_text())['cwd']
        self.assertEqual(self.bootstrap(True).returncode, 0)
        self.assertEqual(first, json.loads((self.home / 'launched.json').read_text())['cwd'])

    def test_remote_workspace_uses_private_application_data(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        directory = Path(json.loads((self.home / 'launched.json').read_text())['cwd'])
        self.assertEqual(directory.parent, self.home / '.local/share/remote-dev/workspaces')
        self.assertRegex(directory.name, r'^repo-space-.*-[0-9a-f]{16}$')
        self.assertEqual(directory.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.home / 'remote-local-dev').exists())

    def test_user_owned_remote_instructions_are_preserved(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        directory = Path(json.loads((self.home / 'launched.json').read_text())['cwd'])
        instructions = directory / 'CLAUDE.md'
        instructions.write_text('User-owned instructions\n')
        self.assertNotEqual(self.bootstrap().returncode, 0)
        self.assertEqual(instructions.read_text(), 'User-owned instructions\n')

    def prepare_remote(self, request):
        (self.home / '.claude.json').write_text(
            '{"mcpServers":{"local-dev":{"type":"http","url":"http://127.0.0.1:7676/mcp"}}}')
        (self.home / '.local/bin').mkdir(parents=True, exist_ok=True)
        (self.home / '.local/bin/claude').touch()
        payload = {'action': 'prepare', 'paths': [], 'hostname': 'test-local', 'browser': False}
        return subprocess.run([sys.executable, '-c', app.REMOTE_BOOTSTRAP, json.dumps(payload)],
            env=dict(os.environ, HOME=str(self.home)), input=request, capture_output=True, text=True)

    def test_remote_prepare_installs_private_clipboard_shim(self):
        result = self.prepare_remote(json.dumps({'token': 'secret', 'xclip': '#!/bin/sh\n'}))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.home / '.local/share/remote-dev'
        self.assertEqual((data / 'clipboard.token').read_text(), 'secret')
        self.assertEqual((data / 'bin/xclip').stat().st_mode & 0o777, 0o700)
        self.assertEqual(data.stat().st_mode & 0o777, 0o700)

    def test_remote_prepare_without_token_installs_nothing(self):
        self.assertEqual(self.prepare_remote('').returncode, 0)
        self.assertFalse((self.home / '.local/share/remote-dev/bin').exists())

    def test_unrelated_remote_mcp_is_preserved(self):
        config = self.home / '.claude.json'
        original = '{"mcpServers":{"local-dev":{"type":"stdio","command":"other"}}}'
        config.write_text(original)
        self.assertNotEqual(self.bootstrap().returncode, 0)
        self.assertEqual(config.read_text(), original)

    class FakeRemote:
        def __init__(self, log, label, diverged=(), fail=None):
            self.log, self.label, self.diverged, self.fail = log, label, list(diverged), fail

        def pull(self, store, key):
            self.log.append(('pull', self.label))
            if self.fail:
                raise RuntimeError(self.fail)
            return self.diverged

        def push(self, store, key):
            self.log.append(('push', self.label))

    def remotes(self, log, **options):
        def transcripts(values, name):
            return self.FakeRemote(log, values['host'], **options.get(values['host'], {}))
        return patch.object(self.c, 'transcripts', side_effect=transcripts)

    def store(self):
        return app.sessions.Store(self.c.data / 'sessions', app.sessions.workspace_name(socket.gethostname(),
                                                                                       [str(self.home)]))

    def test_open_syncs_before_launch_and_after_exit(self):
        self.sync_patch.stop()
        self.connection()
        log = []
        def channel(value, remote, tty=True):
            log.append(('launch', json.loads(shlex.split(remote)[3])['workspace']))
            return ['/bin/sh', '-c', 'exit 3']
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', side_effect=channel), self.remotes(log), \
                patch.dict(os.environ, {'REMOTE_DEV_SYNC_INTERVAL': '0'}):
            self.assertEqual(self.c.open([str(self.home)]), 3)
        name = self.store().dir.name
        self.assertEqual(log, [('pull', 'user@host'), ('push', 'user@host'), ('launch', name), ('pull', 'user@host')])
        self.assertEqual(self.store().pending(), {}, 'a clean final pull clears the mark')

    def test_open_keeps_pending_mark_when_the_final_pull_fails(self):
        self.sync_patch.stop()
        self.connection()
        log, err = [], io.StringIO()
        sync = self.c.sync_open.__func__
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/true']), self.remotes(log), \
                patch.object(app.sys, 'stderr', err):
            real = sync(self.c, self.c.read_connection(self.key), self.store().dir.name)
            real.remote = self.FakeRemote(log, 'user@host', fail='network is unreachable')
            with patch.object(self.c, 'sync_open', return_value=real):
                self.c.open([str(self.home)])
        self.assertEqual(self.store().pending(), {self.key: 'user@host:22'})
        self.assertIn('network is unreachable; run remote-dev sync -H user@host later', err.getvalue())

    def test_open_pulls_pending_servers_first_and_warns_about_disconnected_ones(self):
        self.sync_patch.stop()
        a, b = self.two_connections()
        store = self.store()
        store.set_pending(b['key'], 'nexcom')
        store.set_pending('gone', 'old-server')
        log, err = [], io.StringIO()
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/true']), \
                self.remotes(log, **{'user@host': {'diverged': ['s.jsonl']}}), \
                patch.object(app.sys, 'stderr', err), patch.dict(os.environ, {'REMOTE_DEV_SYNC_INTERVAL': '0'}):
            self.c.open([str(self.home)], host='user@host')
        self.assertEqual(log[:3], [('pull', 'me@nexcom23'), ('pull', 'user@host'), ('push', 'user@host')])
        self.assertEqual(store.pending(), {'gone': 'old-server'})
        self.assertIn('transcripts on old-server may be newer', err.getvalue())
        self.assertIn('transcript s.jsonl on user@host:22 diverged', err.getvalue())

    def test_open_without_rsync_still_opens(self):
        self.sync_patch.stop()
        self.connection()
        err = io.StringIO()
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/true']), \
                patch.object(app.shutil, 'which', return_value=None), patch.object(app.sys, 'stderr', err):
            self.assertEqual(self.c.open([str(self.home)]), 0)
        self.assertIn('rsync not found', err.getvalue())

    def test_invalid_sync_interval_fails_before_launch(self):
        self.sync_patch.stop()
        self.connection()
        log = []
        with patch.object(self.c, 'connection_ready', return_value=True), self.remotes(log), \
                patch.object(self.c, 'channel_command', side_effect=AssertionError('launched')), \
                patch.dict(os.environ, {'REMOTE_DEV_SYNC_INTERVAL': '5m'}):
            with self.assertRaisesRegex(ValueError, 'whole seconds'):
                self.c.open([str(self.home)])
        self.assertEqual((log, self.store().pending()), ([], {}))

    def test_periodic_pull_runs_while_claude_runs(self):
        self.sync_patch.stop()
        self.connection()
        log = []
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/sleep', '1.5']), self.remotes(log), \
                patch.dict(os.environ, {'REMOTE_DEV_SYNC_INTERVAL': '1'}):
            self.c.open([str(self.home)])
        self.assertEqual(log, [('pull', 'user@host'), ('push', 'user@host'), ('pull', 'user@host'),
                               ('pull', 'user@host')], 'before launch, once while running, after exit')

    def test_live_session_keeps_the_pending_mark(self):
        self.connection()
        process = self.child()
        name = self.store().dir.name
        self.session('busy', process, workspace=name)
        sync = app.Sync(self.c, self.store(), self.c.read_connection(self.key), self.FakeRemote([], 'x'), name)
        self.store().set_pending(self.key, 'user@host')
        sync.finish()
        self.assertEqual(self.store().pending(), {self.key: 'user@host'})

    def test_sync_command_pulls_all_then_pushes_and_clears_marks(self):
        self.sync_patch.stop()
        a, b = self.two_connections()
        store = self.store()
        store.set_pending(a['key'], 'a')
        store.set_pending('gone', 'old-server')
        log, out = [], io.StringIO()
        self.assertEqual(app.parser().parse_args(['sync', '-H', 'nex']).host, 'nex')
        with patch.object(self.c, 'connection_ready', return_value=True), self.remotes(log), \
                patch.object(Path, 'cwd', return_value=self.home), patch.object(app.sys, 'stdout', out):
            self.c.sync()
        self.assertEqual(log, [('pull', 'me@nexcom23'), ('pull', 'user@host'),
                               ('push', 'me@nexcom23'), ('push', 'user@host')])
        self.assertEqual(store.pending(), {'gone': 'old-server'})
        self.assertIn('Not connected, may hold newer transcripts: old-server', out.getvalue())
        with patch.object(app.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'Install rsync'):
                self.c.sync()


if __name__ == '__main__':
    unittest.main()

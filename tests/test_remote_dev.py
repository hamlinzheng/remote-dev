import importlib.machinery
import importlib.util
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

    def connection(self, **overrides):
        value = {'id': 'bridge', 'phase': 'ready', 'host': 'user@host', 'ssh_port': 22,
                 'scope': [str(self.home)], 'browser': False,
                 'service_owned': False, 'browser_owned': False}
        value.update(overrides)
        self.c.write_connection(value)
        return value

    def child(self):
        process = subprocess.Popen(['/bin/sleep', '30'])
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
        self.addCleanup(cleanup)
        return process

    def test_start_requires_host_but_no_project(self):
        with self.assertRaises(SystemExit):
            app.parser().parse_args(['start'])
        args = app.parser().parse_args(['start', '--host', 'user@host'])
        self.assertEqual(args.host, 'user@host')
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
        value['browser'] = True
        self.assertIn('127.0.0.1:9222:127.0.0.1:9222', self.c.master_command(value))

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
                self.assertFalse(self.c.connection_file.exists())

    def test_status_read_tolerates_concurrent_connection_removal(self):
        self.connection()
        with patch.object(Path, 'read_text', side_effect=FileNotFoundError):
            self.assertIsNone(self.c.read_connection())

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
        self.assertEqual(self.c.read_connection()['id'], 'bridge')
        self.assertEqual(list(self.c.sessions_dir.glob('*.json')), [])

    def test_open_outside_scope_does_not_restart_or_expand_service(self):
        self.connection(scope=[str(self.home / 'inside')])
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'systemctl', side_effect=AssertionError('service restarted')):
            with self.assertRaises(ValueError):
                self.c.open([str(self.home)])
        self.assertEqual(self.c.read_connection()['scope'], [str(self.home / 'inside')])

    def test_open_exit_removes_only_its_own_session(self):
        self.connection()
        other = self.child()
        app.atomic_json(self.c.sessions_dir / 'other.json', {
            'id': 'other', 'client': 'claude', 'paths': [str(self.home)],
            'ssh': app.process_record(other.pid)})
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/true']):
            self.c.open([str(self.home)])
        self.assertIsNone(other.poll())
        self.assertTrue((self.c.sessions_dir / 'other.json').exists())
        self.assertEqual(self.c.read_connection()['id'], 'bridge')

    def test_stop_closes_all_owned_channels_but_not_stale_pid(self):
        self.connection()
        children = [self.child(), self.child(), self.child()]
        for index, process in enumerate(children):
            record = app.process_record(process.pid)
            if index == 2:
                record['start'] = 'stale'
            app.atomic_json(self.c.sessions_dir / ('%s.json' % index), {
                'id': str(index), 'client': 'claude', 'paths': [], 'ssh': record})
        with patch.object(self.c, 'control', return_value=subprocess.CompletedProcess([], 1, '', '')):
            self.c.cleanup('bridge')
        self.assertIsNotNone(children[0].poll())
        self.assertIsNotNone(children[1].poll())
        self.assertIsNone(children[2].poll())
        self.assertFalse(self.c.connection_file.exists())

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
        self.assertFalse(self.c.connection_file.exists())

    def test_cleanup_preserves_borrowed_browser(self):
        marker = self.home / 'browser-alive'
        marker.touch()
        self.connection(browser=True, browser_owned=False)
        with patch.object(self.c, 'control'), \
                patch.object(self.c.browser, 'stop', side_effect=lambda *args: marker.unlink()):
            self.c.cleanup('bridge')
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
        self.assertFalse(self.c.connection_file.exists())

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
        self.assertTrue(self.c.connection_file.exists())
        self.assertFalse(list(self.c.sessions_dir.glob('*.json')))

    def test_watcher_recovers_all_channels_after_master_loss(self):
        self.connection()
        child = self.child()
        app.atomic_json(self.c.sessions_dir / 'one.json', {
            'id': 'one', 'client': 'claude', 'paths': [], 'ssh': app.process_record(child.pid)})
        with patch.object(self.c, 'connection_ready', return_value=False):
            self.c.watch('bridge')
        self.assertIsNotNone(child.poll())
        self.assertFalse(self.c.connection_file.exists())

    def test_status_lists_each_live_and_stale_session(self):
        self.connection()
        for name, live in [('one', True), ('two', True), ('stale', False)]:
            record = app.process_record(self.child().pid)
            if not live:
                record['start'] = 'stale'
            app.atomic_json(self.c.sessions_dir / (name + '.json'), {
                'id': name, 'client': 'claude', 'paths': [str(self.home)], 'ssh': record})
        with patch.object(self.c, 'service_active', return_value=True), \
                patch.object(self.c, 'control', return_value=subprocess.CompletedProcess([], 0, '', '')):
            result = self.c.status()
        self.assertTrue(result['running'])
        self.assertEqual(result['active_sessions'], 2)
        self.assertEqual(len(result['sessions']), 3)

    def test_repeated_start_reuses_target_and_rejects_changes(self):
        self.connection()
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'master_command', side_effect=AssertionError('second master')):
            self.c.start('user@host', 22, False)
            with self.assertRaises(RuntimeError):
                self.c.start('other@host', 22, False)
        self.assertEqual(self.c.read_connection()['host'], 'user@host')

    def browser_environment(self, *, browser=False, fail_forward=False, timeout_forward=False, interrupt=False):
        self.connection(browser=browser)
        child = self.child()
        app.atomic_json(self.c.sessions_dir / 'existing.json', {
            'id': 'existing', 'client': 'claude', 'paths': [str(self.home)],
            'ssh': app.process_record(child.pid)})
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
        value = self.c.read_connection()
        self.assertTrue(value['browser'])
        self.assertTrue(value['browser_owned'])
        self.assertEqual(value['id'], 'bridge')
        self.assertTrue(browser_alive.exists())
        self.assertTrue(forwarding.exists())
        self.assertIsNone(child.poll())
        self.assertTrue((self.c.sessions_dir / 'existing.json').is_file())
        self.c.start('user@host', 22, True)
        self.assertTrue(self.c.read_connection()['browser_owned'])

    def test_start_reopens_closed_browser_without_adding_duplicate_forward(self):
        child, browser_alive, forwarding = self.browser_environment(browser=True)
        self.c.start('user@host', 22, True)
        self.assertTrue(browser_alive.exists())
        self.assertTrue(forwarding.exists())
        self.assertTrue(self.c.read_connection()['browser_owned'])
        self.assertIsNone(child.poll())

    def test_browser_forward_failure_preserves_connection_and_sessions(self):
        child, browser_alive, forwarding = self.browser_environment(fail_forward=True)
        before = self.c.read_connection()
        with self.assertRaisesRegex(RuntimeError, 'remote port occupied'):
            self.c.start('user@host', 22, True)
        self.assertEqual(self.c.read_connection(), before)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_browser_record_failure_cancels_added_forward(self):
        child, browser_alive, forwarding = self.browser_environment()
        before = self.c.read_connection()
        original = self.c.write_connection
        def write(value):
            if value['browser']:
                raise OSError('disk full')
            return original(value)
        with patch.object(self.c, 'write_connection', side_effect=write):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.c.start('user@host', 22, True)
        self.assertEqual(self.c.read_connection(), before)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_browser_forward_timeout_cancels_uncertain_forward(self):
        child, browser_alive, forwarding = self.browser_environment(timeout_forward=True)
        before = self.c.read_connection()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.c.start('user@host', 22, True)
        self.assertFalse(forwarding.exists())
        self.assertFalse(browser_alive.exists())
        self.assertEqual(self.c.read_connection(), before)
        self.assertIsNone(child.poll())

    def test_interrupted_browser_upgrade_rolls_back_only_new_resources(self):
        child, browser_alive, forwarding = self.browser_environment(interrupt=True)
        before = self.c.read_connection()
        with self.assertRaises(KeyboardInterrupt):
            self.c.start('user@host', 22, True)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertEqual(self.c.read_connection(), before)
        self.assertIsNone(child.poll())

    def test_browser_prepare_failure_does_not_create_local_resources(self):
        child, browser_alive, forwarding = self.browser_environment()
        before = self.c.read_connection()
        with patch.object(app.subprocess, 'run', side_effect=[
                subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CalledProcessError(1, ['ssh'])]):
            with self.assertRaises(subprocess.CalledProcessError):
                self.c.start('user@host', 22, True)
        self.assertEqual(self.c.read_connection(), before)
        self.assertFalse(browser_alive.exists())
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_failed_browser_upgrade_preserves_borrowed_chrome(self):
        child, browser_alive, forwarding = self.browser_environment(fail_forward=True)
        browser_alive.touch()
        with self.assertRaisesRegex(RuntimeError, 'remote port occupied'):
            self.c.start('user@host', 22, True)
        self.assertTrue(browser_alive.exists())
        self.assertFalse(self.c.read_connection()['browser'])
        self.assertFalse(forwarding.exists())
        self.assertIsNone(child.poll())

    def test_stop_holds_lifecycle_lock_until_watcher_is_stopped(self):
        self.connection(watcher_owned=True)
        interleaved = self.home / 'new-start-interleaved'
        def watcher_stop(*args, **kwargs):
            if kwargs.get('unit') == app.WATCH_UNIT:
                try:
                    with self.c.lock('startup.lock', nonblocking=True):
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
            listener.bind(str(self.c.ssh_socket))
        with patch.object(self.c, 'service_active', return_value=False):
            self.c.stop()
        self.assertFalse(self.c.ssh_socket.exists())

    def test_stop_preserves_untracked_active_control_socket(self):
        self.c.runtime.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.c.ssh_socket))
            listener.listen()
            with patch.object(self.c, 'service_active', return_value=False):
                with self.assertRaisesRegex(RuntimeError, 'Untracked.*socket'):
                    self.c.stop()
            self.assertTrue(self.c.ssh_socket.exists())

    def test_stop_preserves_unexpected_file_at_control_socket_path(self):
        self.c.runtime.mkdir(parents=True)
        self.c.ssh_socket.write_text('user-owned file')
        with patch.object(self.c, 'service_active', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'socket'):
                self.c.stop()
        self.assertEqual(self.c.ssh_socket.read_text(), 'user-owned file')

    def test_session_record_write_failure_reaps_channel_not_connection(self):
        self.connection()
        with patch.object(self.c, 'connection_ready', return_value=True), \
                patch.object(self.c, 'channel_command', return_value=['/bin/sleep', '30']), \
                patch.object(app, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.c.open([str(self.home)])
        self.assertTrue(self.c.connection_file.exists())
        self.assertFalse(list(self.c.sessions_dir.glob('*.json')))

    def bootstrap(self, browser=False):
        executable = self.home / '.local/bin/claude'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text('#!' + sys.executable + '\nimport pathlib,os,json\n'
            'pathlib.Path(os.environ["HOME"],"launched.json").write_text('
            'json.dumps({"cwd":str(pathlib.Path.cwd())}))\n')
        executable.chmod(0o755)
        config = self.home / '.claude.json'
        if not config.exists():
            config.write_text('{"mcpServers":{"local-dev":{"type":"http","url":"http://127.0.0.1:7676/mcp"}}}')
        payload = {'action': 'open', 'paths': ['/tmp/repo space;$(touch SHOULD_NOT_EXIST)'],
                   'hostname': 'test-local', 'browser': browser}
        return subprocess.run([sys.executable, '-c', app.REMOTE_BOOTSTRAP, json.dumps(payload)],
            env=dict(os.environ, HOME=str(self.home)), capture_output=True, text=True)

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

    def test_unrelated_remote_mcp_is_preserved(self):
        config = self.home / '.claude.json'
        original = '{"mcpServers":{"local-dev":{"type":"stdio","command":"other"}}}'
        config.write_text(original)
        self.assertNotEqual(self.bootstrap().returncode, 0)
        self.assertEqual(config.read_text(), original)


if __name__ == '__main__':
    unittest.main()

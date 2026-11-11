"""Dedicated local Chrome lifecycle; SSH is managed by remote-dev."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

from lib.common import atomic_json, private_dir, process_record


def managed_browser_pids(settings, proc_root=Path('/proc')):
    """Select only this user's main Chrome process with our profile and port."""
    selected = []
    profile = '--user-data-dir=' + settings['profile']
    port = '--remote-debugging-port=' + str(settings['local_port'])
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = [a for a in (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0') if a]
            if not args:
                continue
            executable = Path(args[0].split(' ', 1)[0]).name
            has_profile, has_port = profile in args, port in args
            is_child = any(arg.startswith('--type=') for arg in args)
            # Chrome may rewrite /proc/cmdline into a single process-title string.
            if len(args) == 1:
                title = args[0]
                has_profile = bool(re.search(r'(?:^| )' + re.escape(profile) + r'(?= --|$)', title))
                has_port = bool(re.search(r'(?:^| )' + re.escape(port) + r'(?= |$)', title))
                is_child = bool(re.search(r'(?:^| )--type=', title))
            if (executable in ('chrome', 'google-chrome', 'chromium', 'chromium-browser')
                    and has_profile and has_port and not is_child):
                selected.append(int(entry.name))
        except (OSError, IndexError):
            continue
    return sorted(selected)


class Controller:
    def __init__(self, home, state_dir=None):
        self.state_dir = Path(state_dir or Path(home) / '.local/state/remote-dev/browser')
        self.session_file = self.state_dir / 'session.json'
        self.values = {'profile': str(Path(home) / '.local/share/chrome-agent-dev'),
                       'local_port': 9222, 'chrome': 'google-chrome'}

    @contextlib.contextmanager
    def lock(self):
        private_dir(self.state_dir)
        with (self.state_dir / 'control.lock').open('a') as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def read_session(self):
        try:
            return json.loads(self.session_file.read_text())
        except FileNotFoundError:
            return None

    def start(self):
        with self.lock():
            settings = dict(self.values)
            previous = self.read_session()
            created = self.start_browser(settings)
            try:
                if created or not previous:
                    atomic_json(self.session_file, settings)
                result = self.status()
                if not result['chrome_running']:
                    raise RuntimeError('Dedicated Chrome exited during startup')
                result.update(created_browser=created, session_stamp=self.session_stamp())
                return result
            except BaseException:
                if created:
                    self.stop_browser(settings)
                if not previous:
                    self.session_file.unlink(missing_ok=True)
                raise

    def stop(self, expected_stamp=None):
        with self.lock():
            if expected_stamp is not None and expected_stamp != self.session_stamp():
                return self.status()
            settings = self.read_session()
            if settings and not self.stop_browser(settings):
                raise RuntimeError('Dedicated Chrome did not stop; inspect remote-dev status')
            self.session_file.unlink(missing_ok=True)
            return self.status()

    def status(self):
        settings = self.read_session()
        return {'chrome_running': bool(settings and managed_browser_pids(settings)
                                       and self.browser_ready(settings))}

    @staticmethod
    def browser_ready(settings):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open('http://127.0.0.1:%d/json/version' % settings['local_port'], timeout=1) as response:
                data = json.load(response)
            return bool(data.get('Browser') and data.get('webSocketDebuggerUrl'))
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def start_browser(self, settings):
        pids = managed_browser_pids(settings)
        if pids and self.browser_ready(settings):
            return False
        if pids:
            raise RuntimeError('Dedicated Chrome is running but its debugging port is not ready; close its windows before retrying')
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', settings['local_port'])) == 0:
                raise RuntimeError('The local debugging port is occupied; no browser was started or stopped')
        if not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY'):
            raise RuntimeError('No desktop display is available; run start from a local desktop terminal')
        binary = shutil.which(settings['chrome'])
        if not binary:
            raise RuntimeError('Chrome executable not found: ' + settings['chrome'])
        Path(settings['profile']).mkdir(parents=True, exist_ok=True, mode=0o700)
        logs = self.state_dir / 'logs'
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (logs / 'chrome.log').open('ab') as log:
            process = subprocess.Popen([
                binary, '--remote-debugging-port=' + str(settings['local_port']),
                '--remote-debugging-address=127.0.0.1',
                '--user-data-dir=' + settings['profile'],
                '--no-first-run', '--no-default-browser-check',
            ], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_UNBLOCK,
                    {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}))
        settings['processes'] = [record for record in [process_record(process.pid, include_command=False)] if record]
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if self.browser_ready(settings) and managed_browser_pids(settings):
                    return True
                if process.poll() is not None:
                    break
                time.sleep(0.15)
            raise RuntimeError('Chrome failed to start; see ' + str(logs / 'chrome.log'))
        except (Exception, KeyboardInterrupt):
            self.stop_browser(settings)
            raise

    def stop_browser(self, settings):
        records = settings.get('processes', [])
        def owned_pids():
            return [pid for pid in managed_browser_pids(settings)
                    if any(record['pid'] == pid and process_record(pid, include_command=False) == record for record in records)]
        for pid in owned_pids():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not owned_pids():
                return True
            time.sleep(0.1)
        return not owned_pids()

    def session_stamp(self):
        if not self.session_file.exists():
            return None
        info = self.session_file.stat()
        return '%s:%s' % (info.st_ino, info.st_mtime_ns)

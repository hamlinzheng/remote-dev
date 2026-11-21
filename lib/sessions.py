"""One local copy of each workspace's Claude transcripts, shared by all servers."""
import contextlib
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess

from lib.common import atomic_json, private_dir

INTERVAL = 300
TRANSCRIPTS = ['--include=*/', '--include=*.jsonl', '--exclude=*', '--append-verify']
OTHERS = ['--exclude=*.jsonl']

# Remote: create and print the Claude project directory of workspace argv[1].
PROJECT_DIR = r'''import os, pathlib, re, sys
home = pathlib.Path.home()
cwd = os.path.realpath(home / '.local/share/remote-dev/workspaces' / sys.argv[1])
base = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
directory = base / 'projects' / re.sub(r'[^A-Za-z0-9]', '-', cwd)
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
print(directory)
'''


def workspace_name(hostname, paths):
    """Remote workspace directory name; the same on every server."""
    identity = json.dumps({'hostname': hostname, 'paths': paths}, sort_keys=True).encode()
    name = Path(paths[0]).name if len(paths) == 1 else 'multi-repo'
    label = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('.-')[:48] or 'workspace'
    return label + '-' + hashlib.sha256(identity).hexdigest()[:16]


def signature(path):
    status = path.stat()
    return [status.st_size, status.st_mtime_ns]


def files(root):
    return {path.relative_to(root).as_posix(): path for path in sorted(root.rglob('*')) if path.is_file()}


def merge_transcript(source, target):
    """Extend target to source's complete lines; False if they diverged."""
    data = source.read_bytes()
    data = data[:data.rfind(b'\n') + 1]
    if not target.exists():
        private_dir(target.parent)
        target.write_bytes(data)
        return True
    current = target.read_bytes()
    if data.startswith(current):
        with target.open('ab') as stream:
            stream.write(data[len(current):])
        return True
    return current.startswith(data)


class Store:
    """main/ is the merged copy; hosts/<key>/ mirrors each server."""

    def __init__(self, root, name):
        self.dir = Path(root) / name
        self.main = self.dir / 'main'

    def mirror(self, key):
        return self.dir / 'hosts' / key

    def conflicts(self, key):
        return self.dir / 'conflicts' / key

    @contextlib.contextmanager
    def lock(self):
        private_dir(self.dir)
        with (self.dir / '.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def pending(self):
        try:
            return json.loads((self.dir / 'pending.json').read_text())
        except FileNotFoundError:
            return {}

    def set_pending(self, key, label=None):
        """Mark a server as possibly ahead of main; no label clears the mark."""
        pending = self.pending()
        if label:
            pending[key] = label
        else:
            pending.pop(key, None)
        atomic_json(self.dir / 'pending.json', pending)

    def state_file(self, key, suffix='.json'):
        return self.dir / 'state' / (key + suffix)

    def read_state(self, key):
        try:
            return json.loads(self.state_file(key).read_text())
        except FileNotFoundError:
            return {}

    def merge(self, key):
        """Merge a mirror into main; return diverged transcripts, copied to conflicts/."""
        state, diverged = self.read_state(key), []
        for name, source in files(self.mirror(key)).items():
            target = self.main / name
            if state.get(name) == signature(source) and target.exists():
                continue
            if source.suffix == '.jsonl':
                if not merge_transcript(source, target):
                    conflict = self.conflicts(key) / name
                    private_dir(conflict.parent)
                    shutil.copy2(source, conflict)
                    diverged.append(name)
            elif not target.exists() or source.stat().st_mtime_ns > target.stat().st_mtime_ns:
                private_dir(target.parent)
                shutil.copy2(source, target)
            state[name] = signature(source)
        atomic_json(self.state_file(key), state)
        return diverged

    def conflicted(self, key):
        directory = self.conflicts(key)
        return sorted(files(directory)) if directory.is_dir() else []

    def behind(self, key):
        """(transcripts, others): which push passes the server needs."""
        mirror, skip, transcripts, others = self.mirror(key), set(self.conflicted(key)), False, False
        for name, source in files(self.main).items():
            target = mirror / name
            if name in skip:
                continue
            if source.suffix == '.jsonl':
                transcripts = transcripts or not target.exists() or source.stat().st_size > target.stat().st_size
            else:
                others = others or not target.exists() or source.stat().st_mtime_ns > target.stat().st_mtime_ns
        return transcripts, others

    def refresh(self, key, skip):
        """Record that the server now matches main, except skipped files."""
        mirror, state = self.mirror(key), self.read_state(key)
        for name, source in files(self.main).items():
            if name in skip:
                continue
            target = mirror / name
            if not target.exists() or signature(target) != signature(source):
                private_dir(target.parent)
                shutil.copy2(source, target)
            state[name] = signature(target)
        atomic_json(self.state_file(key), state)


class Remote:
    """rsync of the remote project directory over the SSH master."""

    def __init__(self, ssh, host, name, cache=None):
        self.ssh, self.host, self.name, self.cache = ssh, host, name, cache
        self.directory = None

    @staticmethod
    def run(args, timeout):
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, start_new_session=True)
        if result.returncode:
            lines = (result.stderr or result.stdout).strip().splitlines()
            raise RuntimeError(lines[-1] if lines else '%s exited with %d' % (args[0], result.returncode))
        return result.stdout

    def location(self):
        """host:dir/ of the remote project directory, cached after the first query."""
        if self.directory is None and self.cache and self.cache.exists():
            self.directory = self.cache.read_text().strip()
        if self.directory is None:
            command = shlex.join(['python3', '-c', PROJECT_DIR, self.name])
            self.directory = self.run(self.ssh + [self.host, command], 60).strip()
            if self.cache:
                private_dir(self.cache.parent)
                self.cache.write_text(self.directory + '\n')
        return '%s:%s/' % (self.host, self.directory)

    def rsync(self, source, target, options):
        self.run(['rsync', '-rtzs', '--timeout=60', '-e', shlex.join(self.ssh), *options, source, target], 600)

    def pull(self, store, key):
        """Mirror the server in one pass, then merge into main."""
        mirror = store.mirror(key)
        private_dir(mirror)
        try:
            self.rsync(self.location(), str(mirror) + '/', [])
        except RuntimeError:
            if not (self.cache and self.cache.exists()):
                raise
            self.cache.unlink()  # possibly stale; query once more
            self.directory = None
            self.rsync(self.location(), str(mirror) + '/', [])
        return store.merge(key)

    def push(self, store, key):
        """Send what the server lacks; never shortens or touches diverged transcripts."""
        transcripts, others = store.behind(key)
        skip = store.conflicted(key)
        excluded = ['--exclude=/' + name for name in skip]
        if transcripts:
            self.rsync(str(store.main) + '/', self.location(), excluded + TRANSCRIPTS)
        if others:
            self.rsync(str(store.main) + '/', self.location(), excluded + OTHERS + ['--update'])
        if transcripts or others:
            store.refresh(key, set(skip))

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import sessions  # noqa: E402


class MergeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = sessions.Store(self.root / 'sessions', 'repo-0123456789abcdef')
        self.mirror = self.store.mirror('k')

    def put(self, base, name, data, mtime=None):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_workspace_name_matches_existing_remote_directories(self):
        self.assertEqual(sessions.workspace_name('test-local', ['/tmp/repo space']),
                         'repo-space-037004853188f772')
        self.assertTrue(sessions.workspace_name('h', ['/a', '/b']).startswith('multi-repo-'))

    def test_transcript_appends_only_complete_new_lines(self):
        self.put(self.store.main, 's.jsonl', b'1\n')
        self.put(self.mirror, 's.jsonl', b'1\n2\n3-partial')
        self.assertEqual(self.store.merge('k'), [])
        self.assertEqual((self.store.main / 's.jsonl').read_bytes(), b'1\n2\n')

    def test_main_ahead_of_server_is_kept(self):
        self.put(self.store.main, 's.jsonl', b'1\n2\n')
        self.put(self.mirror, 'sub/s.jsonl', b'x\n')
        self.put(self.mirror, 's.jsonl', b'1\n')
        self.assertEqual(self.store.merge('k'), [])
        self.assertEqual((self.store.main / 's.jsonl').read_bytes(), b'1\n2\n')
        self.assertEqual((self.store.main / 'sub/s.jsonl').read_bytes(), b'x\n')

    def test_diverged_transcript_is_kept_aside_once(self):
        self.put(self.store.main, 's.jsonl', b'1\nmain\n')
        self.put(self.mirror, 's.jsonl', b'1\nserver\n')
        self.assertEqual(self.store.merge('k'), ['s.jsonl'])
        self.assertEqual((self.store.main / 's.jsonl').read_bytes(), b'1\nmain\n')
        self.assertEqual((self.store.conflicts('k') / 's.jsonl').read_bytes(), b'1\nserver\n')
        self.assertEqual(self.store.conflicted('k'), ['s.jsonl'])
        self.assertEqual(self.store.merge('k'), [], 'unchanged mirror files are not merged again')

    def test_other_files_take_the_newer_version(self):
        self.put(self.store.main, 'memory/a.md', b'main', mtime=2000)
        self.put(self.mirror, 'memory/a.md', b'older', mtime=1000)
        self.put(self.store.main, 'memory/b.md', b'main', mtime=1000)
        self.put(self.mirror, 'memory/b.md', b'newer', mtime=2000)
        self.store.merge('k')
        self.assertEqual((self.store.main / 'memory/a.md').read_bytes(), b'main')
        self.assertEqual((self.store.main / 'memory/b.md').read_bytes(), b'newer')

    def test_pending_marks(self):
        self.store.set_pending('a', 'host-a')
        self.store.set_pending('b', 'host-b')
        self.store.set_pending('a')
        self.assertEqual(self.store.pending(), {'b': 'host-b'})


@unittest.skipUnless(shutil.which('rsync'), 'rsync is required')
class RemoteTests(unittest.TestCase):
    """Real rsync through a fake ssh that runs the remote side locally under another HOME."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.name = 'repo-0123456789abcdef'
        self.store = sessions.Store(self.root / 'local', self.name)

    def server(self, label):
        home = self.root / label
        (home / '.local/share/remote-dev/workspaces' / self.name).mkdir(parents=True)
        ssh = self.root / ('ssh-' + label)
        ssh.write_text('#!/bin/sh\n[ "$1" = -l ] && shift 2\nshift\n'  # rsync passes -l USER HOST
                       'HOME=%s; export HOME\nunset CLAUDE_CONFIG_DIR\nexec sh -c "$*"\n' % home)
        ssh.chmod(0o755)
        remote = sessions.Remote([str(ssh)], 'user@' + label, self.name)
        remote.location()
        return remote, Path(remote.directory)

    def test_turns_between_two_servers(self):
        a, a_dir = self.server('a')
        b, b_dir = self.server('b')
        self.assertTrue(str(a_dir).startswith(str(self.root / 'a/.claude/projects/')))
        (a_dir / 's.jsonl').write_bytes(b'1\n')
        (a_dir / 'memory').mkdir()
        (a_dir / 'memory/MEMORY.md').write_text('note')
        self.assertEqual(a.pull(self.store, 'a'), [])
        with (a_dir / 's.jsonl').open('ab') as stream:
            stream.write(b'2\n')
        a.pull(self.store, 'a')
        b.pull(self.store, 'b')
        b.push(self.store, 'b')
        self.assertEqual((b_dir / 's.jsonl').read_bytes(), b'1\n2\n')
        self.assertEqual((b_dir / 'memory/MEMORY.md').read_text(), 'note')
        with (b_dir / 's.jsonl').open('ab') as stream:
            stream.write(b'3\n')
        b.pull(self.store, 'b')
        a.pull(self.store, 'a')
        a.push(self.store, 'a')
        self.assertEqual((a_dir / 's.jsonl').read_bytes(), b'1\n2\n3\n')

    def test_push_never_shortens_or_overwrites_server_transcripts(self):
        a, a_dir = self.server('a')
        (a_dir / 'grow.jsonl').write_bytes(b'1\n')
        (a_dir / 'fork.jsonl').write_bytes(b'1\nserver\n')
        (self.store.main).mkdir(parents=True)
        (self.store.main / 'fork.jsonl').write_bytes(b'1\nmain\n')
        self.assertEqual(a.pull(self.store, 'a'), ['fork.jsonl'])
        with (a_dir / 'grow.jsonl').open('ab') as stream:
            stream.write(b'2\n')  # Claude keeps writing between pull and push
        a.push(self.store, 'a')
        self.assertEqual((a_dir / 'grow.jsonl').read_bytes(), b'1\n2\n')
        self.assertEqual((a_dir / 'fork.jsonl').read_bytes(), b'1\nserver\n')
        a.pull(self.store, 'a')
        self.assertEqual((self.store.main / 'grow.jsonl').read_bytes(), b'1\n2\n')

    def test_push_runs_only_when_the_server_is_behind(self):
        a, a_dir = self.server('a')
        (a_dir / 's.jsonl').write_bytes(b'1\n')
        a.pull(self.store, 'a')
        calls = []
        real = a.rsync
        def rsync(*args):
            calls.append(args[2])
            return real(*args)
        a.rsync = rsync
        a.push(self.store, 'a')
        self.assertEqual(calls, [], 'a server that holds main gets no transfer')
        with (self.store.main / 's.jsonl').open('ab') as stream:
            stream.write(b'2\n')
        a.push(self.store, 'a')
        self.assertEqual(calls, [sessions.TRANSCRIPTS])
        self.assertEqual((a_dir / 's.jsonl').read_bytes(), b'1\n2\n')
        a.pull(self.store, 'a')
        calls.clear()
        a.push(self.store, 'a')
        self.assertEqual(calls, [], 'pull after push does not cause another push')

    def test_stale_cached_location_is_queried_again(self):
        a, a_dir = self.server('a')
        cache = self.root / 'cache'
        cache.write_text(str(self.root / 'gone') + '\n')
        remote = sessions.Remote(a.ssh, a.host, self.name, cache)
        (a_dir / 's.jsonl').write_bytes(b'1\n')
        remote.pull(self.store, 'a')
        self.assertEqual(cache.read_text().strip(), str(a_dir))
        self.assertEqual((self.store.main / 's.jsonl').read_bytes(), b'1\n')

    def test_failures_raise_one_line(self):
        remote = sessions.Remote(['false'], 'user@x', self.name)
        with self.assertRaisesRegex(RuntimeError, 'exited with 1'):
            remote.pull(self.store, 'x')


if __name__ == '__main__':
    unittest.main()

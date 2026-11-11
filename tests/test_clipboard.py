import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import clipboard

PNG = b'\x89PNG\r\n\x1a\nimage-bytes'


def fake_xclip(target):
    return {'TARGETS': b'TARGETS\nUTF8_STRING\ntext/plain\nimage/png\n', 'image/png': PNG}.get(target, b'')


class ClipboardTests(unittest.TestCase):
    def test_answer_exposes_only_image_targets_and_data(self):
        with patch.object(clipboard, 'xclip', side_effect=fake_xclip):
            self.assertEqual(clipboard.answer(b'token TARGETS', b'token'), b'image/png\n')
            self.assertEqual(clipboard.answer(b'token image/png', b'token'), PNG)
            self.assertEqual(clipboard.answer(b'token UTF8_STRING', b'token'), b'')
            self.assertEqual(clipboard.answer(b'wrong image/png', b'token'), b'')

    def test_shim_reads_image_through_forwarded_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.local/share/remote-dev').mkdir(parents=True)
            (home / '.local/share/remote-dev/clipboard.token').write_text('token\n')
            with patch.object(clipboard, 'xclip', side_effect=fake_xclip):
                server = clipboard.serve(home / 'clipboard.sock', 'token')
                self.addCleanup(server.close)
                port = self.relay(home / 'clipboard.sock')
                shim = home / 'xclip'
                shim.write_text(clipboard.SHIM.replace(str(clipboard.PORT), str(port)))
                env = dict(os.environ, HOME=str(home))
                def run(*args):
                    return subprocess.run([sys.executable, str(shim), *args], env=env, capture_output=True)
                self.assertEqual(run('-selection', 'clipboard', '-t', 'TARGETS', '-o').stdout, b'image/png\n')
                result = run('-selection', 'clipboard', '-t', 'image/png', '-o')
                self.assertEqual((result.returncode, result.stdout), (0, PNG))
                self.assertEqual(run('-selection', 'clipboard', '-t', 'image/bmp', '-o').returncode, 1)
                self.assertEqual(run('-selection', 'clipboard').returncode, 1)

    def test_server_thread_leaves_signals_to_main_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            server = clipboard.serve(Path(directory) / 'clipboard.sock', 'token')
            self.addCleanup(server.close)
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(0.1)
                self.assertIn(signal.SIGINT, signal.sigpending())
            finally:
                with self.assertRaises(KeyboardInterrupt):
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous)
                    time.sleep(0.1)

    def relay(self, path):
        """Stand in for ssh -R: forward one TCP port to the Unix socket."""
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        self.addCleanup(listener.close)
        def loop():
            while True:
                try:
                    client, _ = listener.accept()
                except OSError:
                    return
                with client, socket.socket(socket.AF_UNIX) as upstream:
                    upstream.connect(str(path))
                    upstream.sendall(client.recv(256))
                    client.sendall(b''.join(iter(lambda: upstream.recv(65536), b'')))
        clipboard.background(loop)
        return listener.getsockname()[1]


if __name__ == '__main__':
    unittest.main()

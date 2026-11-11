"""Serve local clipboard images to the remote xclip shim; text is never exposed."""
import contextlib
import hmac
import re
import signal
import socket
import subprocess
import threading

PORT = 7677
IMAGE = re.compile(rb'image/(png|jpeg|jpg|gif|webp|bmp)')

# Installed remotely as xclip; answers only Claude's image paste calls.
SHIM = r'''#!/usr/bin/env python3
import pathlib, socket, sys
args = sys.argv[1:]
if len(args) != 5 or args[:3] != ['-selection', 'clipboard', '-t'] or args[4] != '-o':
    sys.exit(1)
try:
    token = (pathlib.Path.home() / '.local/share/remote-dev/clipboard.token').read_text().strip()
    with socket.create_connection(('127.0.0.1', %d), timeout=10) as stream:
        stream.sendall((token + ' ' + args[3] + '\n').encode())
        data = b''.join(iter(lambda: stream.recv(65536), b''))
except OSError:
    sys.exit(1)
sys.stdout.buffer.write(data)
sys.exit(0 if data else 1)
''' % PORT


def xclip(target):
    try:
        result = subprocess.run(['xclip', '-selection', 'clipboard', '-t', target, '-o'],
                                capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return b''
    return result.stdout if result.returncode == 0 else b''


def answer(request, token):
    secret, _, target = request.partition(b' ')
    if not hmac.compare_digest(secret, token):
        return b''
    if target == b'TARGETS':
        return b''.join(line + b'\n' for line in xclip('TARGETS').splitlines() if IMAGE.fullmatch(line))
    return xclip(target.decode()) if IMAGE.fullmatch(target) else b''


def background(target):
    """Start a daemon thread that leaves SIGINT/SIGTERM/SIGHUP to the main thread's defer_signals."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    try:
        threading.Thread(target=target, daemon=True).start()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def serve(path, token):
    """Answer one request per connection in a daemon thread; close the returned socket to stop."""
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()

    def loop():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection, contextlib.suppress(OSError):
                connection.settimeout(5)
                request = connection.makefile('rb').readline(256).strip()
                connection.sendall(answer(request, token.encode()))

    background(loop)
    return listener

"""Private state, process identity and SSH helpers."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def atomic_json(path, value):
    private_dir(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_host(host):
    if host.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_.@:\[\]-]+', host):
        raise ValueError('Invalid SSH target; use [USER@]HOST')


def ssh_options(port=22, identity_file=None):
    if not 1 <= port <= 65535:
        raise ValueError('--ssh-port must be between 1 and 65535')
    args = ['-p', str(port), '-o', 'ForwardAgent=no', '-o', 'ConnectTimeout=10']
    if identity_file:
        identity = Path(identity_file).expanduser().resolve(strict=True)
        if not identity.is_file():
            raise ValueError('SSH identity must be a file: ' + str(identity))
        args += ['-i', str(identity), '-o', 'IdentitiesOnly=yes']
    return args


def process_record(pid, *, include_command=True):
    path = Path('/proc') / str(pid)
    try:
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        if path.stat().st_uid != os.getuid() or fields[0] == 'Z':
            return None
        record = {'pid': pid, 'start': fields[19]}
        if include_command:
            record['command'] = hashlib.sha256((path / 'cmdline').read_bytes()).hexdigest()
        return record
    except (OSError, IndexError):
        return None

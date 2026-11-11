"""Explicit, user-scoped local and remote environment preparation."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

from lib.common import atomic_json, private_dir, ssh_options, validate_host

ROOT = Path(__file__).resolve().parents[1]
MARKER = '# Managed by remote-dev.'
NODE_VERSION = '24.22.2'


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def find_node(data, install_node=False, previous=None, browser=False):
    candidates = [previous, shutil.which('node')]
    candidates += sorted((data / 'node').glob('node-*/bin/node'), reverse=True)
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        node = Path(candidate).resolve()
        try:
            version = subprocess.check_output([str(node), '--version'], text=True).strip()
        except (OSError, subprocess.SubprocessError):
            continue
        try:
            major, minor, patch = map(int, version.lstrip('v').split('.'))
        except ValueError:
            continue
        compatible = (major == 20 and minor >= 19 or major == 22 and minor >= 12 or major >= 23) if browser else major == 24
        if compatible and (node.parent / 'npm').exists():
            return node
    if not install_node:
        requirement = 'Node ^20.19.0 || ^22.12.0 || >=23' if browser else 'Node24'
        raise RuntimeError(requirement + ' and its npm are required; add --with-node for a private installation')
    return download_node(data)


def download_node(data):
    arch = {'x86_64': 'x64', 'aarch64': 'arm64'}.get(platform.machine())
    if platform.system() != 'Linux' or not arch:
        raise RuntimeError('Private Node installation supports Linux x64 and arm64 only')
    name = 'node-v%s-linux-%s' % (NODE_VERSION, arch)
    parent = data / 'node'
    private_dir(parent)
    target = parent / name
    if target.exists():
        raise RuntimeError('Incomplete private Node directory exists; inspect ' + str(target))
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=parent) as temporary:
        stage = Path(temporary)
        filename = name + '.tar.xz'
        base = 'https://nodejs.org/dist/v' + NODE_VERSION + '/'
        with urllib.request.urlopen(base + 'SHASUMS256.txt', timeout=30) as response:
            checksums = response.read().decode().splitlines()
        expected = next(line.split()[0] for line in checksums if line.split()[-1] == filename)
        archive = stage / filename
        with urllib.request.urlopen(base + filename, timeout=120) as response, archive.open('wb') as stream:
            shutil.copyfileobj(response, stream)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError('Node archive checksum mismatch')
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                path = (stage / member.name).resolve()
                path.relative_to(stage)
                if member.issym() or member.islnk():
                    link = (path.parent / member.linkname).resolve() if member.issym() else (stage / member.linkname).resolve()
                    link.relative_to(stage)
                if member.name != name and not member.name.startswith(name + '/'):
                    raise ValueError('Unexpected Node archive member')
            tar.extractall(stage)
        subprocess.run([str(stage / name / 'bin/node'), '--version'], check=True, stdout=subprocess.DEVNULL)
        (stage / name).replace(target)
    return target / 'bin/node'


def install_runtime(source, parent, node):
    digest = hashlib.sha256((source / 'package.json').read_bytes() +
                            (source / 'package-lock.json').read_bytes()).hexdigest()[:16]
    private_dir(parent)
    target = parent / digest
    if (target / '.complete').is_file() and (target / 'node_modules').is_dir():
        return target
    if target.exists():
        raise RuntimeError('Incomplete dependency directory exists; inspect ' + str(target))
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=parent) as temporary:
        stage = Path(temporary)
        for name in ['package.json', 'package-lock.json']:
            shutil.copyfile(source / name, stage / name)
        environment = dict(os.environ, PATH=str(Path(node).parent) + ':' + os.environ.get('PATH', ''))
        subprocess.run([str(node), str(Path(node).parent / 'npm'), 'ci', '--omit=dev',
                        '--no-audit', '--no-fund'], cwd=stage, env=environment, check=True)
        (stage / '.complete').touch(mode=0o600)
        stage.replace(target)
    return target


def remove_owned_paths(data, paths):
    targets = []
    for value in paths:
        path = Path(value).resolve()
        permitted = False
        for parent in [data / 'runtimes', data / 'node']:
            parent = parent.resolve()
            try:
                permitted = path != parent and bool(path.relative_to(parent).parts)
            except ValueError:
                continue
            if permitted:
                break
        if not permitted:
            raise ValueError('Refusing to remove an unowned runtime path: ' + str(path))
        targets.append(path)
    for target in targets:
        if target.exists():
            shutil.rmtree(target)


def unit_text(home, node, executable, config_dir):
    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    environment = subprocess.check_output(['systemctl', '--user', 'show-environment'], text=True)
    manager_path = next((line[5:] for line in environment.splitlines() if line.startswith('PATH=')), '/usr/local/bin:/usr/bin:/bin')
    path = str(node.parent) + ':' + str(home / '.local/bin') + ':' + manager_path
    return '\n'.join([MARKER, '[Unit]', 'Description=On-demand host DevSpace',
                      '[Service]', 'Type=simple', 'UMask=0077', 'Restart=no',
                      'KillMode=control-group', 'TimeoutStopSec=10', 'WorkingDirectory=%h',
                      'Environment=' + quote('DEVSPACE_CONFIG_DIR=' + str(config_dir)),
                      'Environment=' + quote('PATH=' + path),
                      'ExecStart=' + quote(node) + ' ' + quote(executable) + ' serve', ''])


def local_install(app, root, browser=False, install_node=False):
    if app.unit_file.exists() and not app.unit_file.read_text().startswith(MARKER):
        raise RuntimeError('An unrelated remote-dev.service exists; not replaced')
    entry = app.home / '.local/bin/remote-dev'
    source = root / 'remote-dev'
    if (entry.exists() or entry.is_symlink()) and entry.resolve() != source.resolve():
        raise RuntimeError('An unrelated remote-dev executable exists; not replaced')
    record_path = app.data / 'installation.json'
    previous = read_json(record_path)
    if app.config_file.exists() and not previous:
        raise RuntimeError('Unmanaged DevSpace configuration exists; not replaced')
    if browser and not shutil.which('google-chrome'):
        raise RuntimeError('Install google-chrome on the local desktop before browser install')
    config = app.read_config() if app.config_file.exists() else None
    node = find_node(app.data, install_node, previous.get('node'))
    runtime = install_runtime(root / 'runtimes/devspace', app.data / 'runtimes/devspace', node)
    executable = runtime / 'node_modules/@waishnav/devspace/bin/devspace.js'
    text = unit_text(app.home, node, executable, app.config_dir)
    private_dir(app.config_dir)
    auth = app.config_dir / 'auth.json'
    saved = {p: p.read_bytes() if p.exists() else None for p in [app.config_file, auth, app.unit_file, record_path]}
    linked = False
    try:
        if config is None:
            config = {'configVersion': 1,
                      'server': {'host': '127.0.0.1', 'port': 7676, 'publicBaseUrl': 'http://127.0.0.1:7676'},
                      'workspaces': {'allowedRoots': [str(app.home)]},
                      'storage': {'stateDir': str(app.data / 'server')},
                      'activity': {'enabled': False}, 'skills': {'enabled': False},
                      'subagents': {'enabled': False, 'providers': []},
                      'oauth': {'allowedRedirectHosts': ['localhost', '127.0.0.1']}}
        atomic_json(app.config_file, config)
        if not auth.exists():
            environment = dict(os.environ, DEVSPACE_CONFIG_DIR=str(app.config_dir))
            subprocess.run([str(node), str(executable), 'init', '--yes', '--providers', '',
                            '--roots', str(app.data), '--public-url', 'http://127.0.0.1:7676', '--json'],
                           env=environment, check=True, stdout=subprocess.DEVNULL)
            atomic_json(app.config_file, config)
        os.chmod(auth, 0o600)
        private_dir(app.unit_file.parent)
        app.unit_file.write_text(text)
        app.unit_file.chmod(0o600)
        app.systemctl('daemon-reload')
        entry.parent.mkdir(parents=True, exist_ok=True)
        if not entry.is_symlink():
            entry.symlink_to(source)
            linked = True
        owned = set(previous.get('owned_paths', []))
        owned.add(str(runtime))
        if (app.data / 'node').resolve() in node.parents:
            owned.add(str(node.parent.parent))
        atomic_json(record_path, {'managedBy': 'remote-dev', 'interface': 3, 'node': str(node),
                                 'devspace': '1.1.1', 'source': str(source.resolve()),
                                 'owned_paths': sorted(owned)})
    except BaseException:
        for path, value in saved.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(value)
                path.chmod(0o600)
        if linked:
            entry.unlink(missing_ok=True)
        app.systemctl('daemon-reload', check=False)
        raise


def remote_call(root, host, port, identity_file, browser=False, install_node=False, remove=False):
    payload = {'browser': browser, 'install_node': install_node, 'remove': remove}
    bundle = io.BytesIO()
    names = ['lib/__init__.py', 'lib/common.py', 'lib/install.py']
    names += ['runtimes/chrome-devtools/' + name for name in ['package.json', 'package-lock.json']]
    with tarfile.open(fileobj=bundle, mode='w:gz') as archive:
        for name in names:
            archive.add(root / name, arcname=name)
    runner = '''import io,json,pathlib,runpy,sys,tarfile,tempfile
with tempfile.TemporaryDirectory(prefix='remote-dev-install-') as directory:
    stage=pathlib.Path(directory)
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode='r:gz') as archive:
        for member in archive.getmembers():
            (stage/member.name).resolve().relative_to(stage)
            if not member.isfile(): raise ValueError('Unexpected install bundle member')
        archive.extractall(stage)
    sys.path.insert(0,str(stage))
    module=runpy.run_path(str(stage/'lib/install.py'))
    module['remote_install'](json.loads(sys.argv[1]))
'''
    command = shlex.join(['python3', '-c', runner, json.dumps(payload)])
    subprocess.run(['ssh', *ssh_options(port, identity_file), '-T', host, command],
                   input=bundle.getvalue(), check=True)


def claude_command(home):
    command = home / '.local/bin/claude'
    if not command.is_file():
        found = shutil.which('claude')
        if not found:
            raise RuntimeError('Install and authenticate Claude Code on the remote server first')
        command = Path(found)
    return str(command)


def mcp_config(home):
    return read_json(home / '.claude.json').get('mcpServers', {})


def managed_chrome(config, data):
    args = config.get('args', [])
    if config.get('type', 'stdio') != 'stdio' or not args:
        return False
    try:
        Path(args[0]).resolve().relative_to(data.resolve())
    except ValueError:
        return False
    return (args[0].endswith('/chrome-devtools-mcp.js') and
            '--browser-url=http://127.0.0.1:9222' in args)


def remote_install(options):
    home = Path.home()
    data = home / '.local/share/remote-dev'
    private_dir(data)
    with (data / 'install.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if options.get('remove'):
            return remote_uninstall(home, data)
        claude = claude_command(home)
        auth = subprocess.check_output([claude, 'auth', 'status', '--json'], text=True)
        if not json.loads(auth).get('loggedIn'):
            raise RuntimeError('Authenticate remote Claude Code before install')
        record_path = data / 'installation.json'
        previous = read_json(record_path)
        existing = mcp_config(home)
        dev = existing.get('local-dev')
        if dev and (dev.get('type') != 'http' or dev.get('url') != 'http://127.0.0.1:7676/mcp'):
            raise RuntimeError('An unrelated local-dev MCP exists; not replaced')
        chrome = existing.get('local-chrome')
        if options['browser'] and chrome and not managed_chrome(chrome, data):
            raise RuntimeError('An unrelated local-chrome MCP exists; not replaced')
        owned = set(previous.get('owned_paths', []))
        node = previous.get('node') or (chrome.get('command') if chrome and managed_chrome(chrome, data) else None)
        desired = {}
        if not dev:
            desired['local-dev'] = {'type': 'http', 'url': 'http://127.0.0.1:7676/mcp'}
        if options['browser']:
            node = find_node(data, options['install_node'], node, browser=True)
            runtime = install_runtime(ROOT / 'runtimes/chrome-devtools', data / 'runtimes/chrome-devtools', node)
            entry = runtime / 'node_modules/chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js'
            desired['local-chrome'] = {'type': 'stdio', 'command': str(node),
                                      'args': [str(entry), '--browser-url=http://127.0.0.1:9222',
                                               '--no-usage-statistics', '--no-performance-crux'], 'env': {}}
            owned.add(str(runtime))
            if (data / 'node').resolve() in Path(node).parents:
                owned.add(str(Path(node).parent.parent))
        changed = []
        registrations = previous.get('registrations', {})
        try:
            for name, value in desired.items():
                if existing.get(name) == value:
                    continue
                before = existing.get(name)
                if before:
                    subprocess.run([claude, 'mcp', 'remove', '--scope', 'user', name], check=True, stdout=subprocess.DEVNULL)
                changed.append((name, before))
                if name == 'local-dev':
                    args = [claude, 'mcp', 'add', '--transport', 'http', '--scope', 'user',
                            '--callback-port', '8765', name, value['url']]
                else:
                    args = [claude, 'mcp', 'add-json', '--scope', 'user', name, json.dumps(value)]
                subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
                registrations[name] = mcp_config(home)[name]
            atomic_json(record_path, {'managedBy': 'remote-dev', 'node': str(node) if node else None,
                                     'owned_paths': sorted(owned), 'registrations': registrations})
        except BaseException:
            for name, before in reversed(changed):
                if name in mcp_config(home):
                    subprocess.run([claude, 'mcp', 'remove', '--scope', 'user', name], check=False, stdout=subprocess.DEVNULL)
                if before:
                    subprocess.run([claude, 'mcp', 'add-json', '--scope', 'user', name, json.dumps(before)],
                                   check=True, stdout=subprocess.DEVNULL)
            raise
    print('Remote install complete. Claude login and OAuth state were preserved.')


def remote_uninstall(home, data):
    record_path = data / 'installation.json'
    record = read_json(record_path)
    if record.get('managedBy') != 'remote-dev':
        print('No managed remote installation found; existing configuration was preserved.')
        return
    existing = mcp_config(home)
    claude = claude_command(home)
    modified = False
    for name, value in record.get('registrations', {}).items():
        if name not in existing:
            continue
        if existing[name] != value:
            modified = True
            print('Preserved modified MCP registration: ' + name)
            continue
        subprocess.run([claude, 'mcp', 'remove', '--scope', 'user', name], check=True, stdout=subprocess.DEVNULL)
    if not modified:
        remove_owned_paths(data, record.get('owned_paths', []))
        shutil.rmtree(data / 'bin', ignore_errors=True)
        (data / 'clipboard.token').unlink(missing_ok=True)
        record_path.unlink(missing_ok=True)
    print('Remote uninstall complete. Authentication, history and workspaces were preserved.')


def install(app, root, host=None, port=22, identity_file=None, browser=False, install_node=False):
    ssh_options(port, identity_file)
    if host:
        validate_host(host)
    with app.lock('startup.lock', nonblocking=True), app.lock():
        if app.read_connection() or app.service_active():
            raise RuntimeError('Stop shared access before install')
        local_install(app, root, browser, install_node)
        print('Local install complete. DevSpace is stopped; no automatic startup was enabled.', flush=True)
        if host:
            remote_call(root, host, port, identity_file, browser, install_node)
    print('Installation complete. Run remote-dev start with an explicit SSH target.')


def uninstall(app, root, host=None, port=22, identity_file=None):
    ssh_options(port, identity_file)
    if host:
        validate_host(host)
    with app.lock('startup.lock', nonblocking=True), app.lock():
        if host:
            remote_call(root, host, port, identity_file, remove=True)
        record_path = app.data / 'installation.json'
        record = read_json(record_path)
        if record.get('managedBy') != 'remote-dev':
            raise RuntimeError('No managed installation record; existing resources were preserved')
        if app.unit_file.exists():
            if not app.unit_file.read_text().startswith(MARKER):
                raise RuntimeError('The service was replaced; not removed')
            app.unit_file.unlink()
            app.systemctl('daemon-reload')
        entry = app.home / '.local/bin/remote-dev'
        if entry.is_symlink() and str(entry.resolve()) == record.get('source'):
            entry.unlink()
        remove_owned_paths(app.data, record.get('owned_paths', []))
        atomic_json(record_path, {'managedBy': 'remote-dev', 'interface': 3,
                                  'uninstalled': True, 'owned_paths': []})
    print('Uninstalled managed resources. Configuration, OAuth, browser profile and project workloads were preserved.')

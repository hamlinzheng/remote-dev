import json
import io
import hashlib
import tarfile
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import install


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def manifests(self):
        source = self.home / 'source'
        source.mkdir()
        (source / 'package.json').write_text('{"name":"fixture","version":"1.0.0"}')
        (source / 'package-lock.json').write_text('{"lockfileVersion":3}')
        return source

    def test_failed_dependency_install_preserves_existing_runtime(self):
        source = self.manifests()
        parent = self.home / 'runtimes'
        old = parent / 'old'
        old.mkdir(parents=True)
        (old / 'working').touch()
        with patch.object(install.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['npm'])):
            with self.assertRaises(subprocess.CalledProcessError):
                install.install_runtime(source, parent, '/usr/bin/node')
        self.assertTrue((old / 'working').exists())
        self.assertEqual([p.name for p in parent.iterdir()], ['old'])

    def test_locked_runtime_is_reused_only_after_complete_install(self):
        source = self.manifests()
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            stage = Path(kwargs['cwd'])
            (stage / 'node_modules').mkdir()
            (stage / 'node_modules/fixture').write_text('installed')
        with patch.object(install.subprocess, 'run', side_effect=run):
            first = install.install_runtime(source, self.home / 'runtimes', '/usr/bin/node')
            second = install.install_runtime(source, self.home / 'runtimes', '/usr/bin/node')
        self.assertEqual(first, second)
        self.assertTrue((first / 'node_modules/fixture').exists())
        self.assertEqual(len(calls), 1)
        self.assertIn('ci', calls[0])

    def test_private_node_archive_is_verified_and_installed(self):
        name = 'node-v' + install.NODE_VERSION + '-linux-x64'
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:xz') as archive:
            directory = tarfile.TarInfo(name)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            archive.addfile(directory)
            for filename, data in [('bin/node', b'#!/bin/sh\necho v24.22.2\n'), ('bin/npm', b'fixture')]:
                member = tarfile.TarInfo(name + '/' + filename)
                member.size = len(data)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(data))
        payload = raw.getvalue()
        checksum = hashlib.sha256(payload).hexdigest() + '  ' + name + '.tar.xz\n'
        def download(url, **kwargs):
            return io.BytesIO(checksum.encode() if url.endswith('SHASUMS256.txt') else payload)
        with patch.object(install.platform, 'machine', return_value='x86_64'), \
                patch.object(install.urllib.request, 'urlopen', side_effect=download):
            node = install.download_node(self.home / 'app')
        self.assertEqual(subprocess.check_output([str(node), '--version'], text=True).strip(), 'v24.22.2')
        self.assertTrue((node.parent / 'npm').exists())
        self.assertFalse(list(node.parents[2].glob('.stage-*')))

    def test_missing_node_requires_explicit_private_install(self):
        with patch.object(install.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, '--with-node'):
                install.find_node(self.home / 'app', False)
        self.assertFalse((self.home / 'app').exists())

    def test_browser_install_reuses_supported_existing_node_versions(self):
        binary = self.home / 'bin/node'
        binary.parent.mkdir()
        binary.touch()
        (binary.parent / 'npm').touch()
        versions = [('v20.19.0', True), ('v20.20.2', True), ('v20.18.3', False),
                    ('v21.7.0', False), ('v22.11.0', False), ('v22.12.0', True),
                    ('v24.22.2', True)]
        for version, supported in versions:
            with self.subTest(version=version), \
                    patch.object(install.shutil, 'which', return_value=str(binary)), \
                    patch.object(install.subprocess, 'check_output', return_value=version):
                if supported:
                    self.assertEqual(install.find_node(self.home / 'app', browser=True), binary)
                else:
                    with self.assertRaises(RuntimeError):
                        install.find_node(self.home / 'app', browser=True)

    def test_local_devspace_still_rejects_node20(self):
        binary = self.home / 'bin/node'
        binary.parent.mkdir()
        binary.touch()
        (binary.parent / 'npm').touch()
        with patch.object(install.shutil, 'which', return_value=str(binary)), \
                patch.object(install.subprocess, 'check_output', return_value='v20.20.2'):
            with self.assertRaisesRegex(RuntimeError, 'Node24'):
                install.find_node(self.home / 'app')

    def test_remove_owned_paths_refuses_outside_application_runtime(self):
        data = self.home / 'app'
        outside = self.home / 'other-runtime'
        outside.mkdir()
        (outside / 'keep').touch()
        with self.assertRaises(ValueError):
            install.remove_owned_paths(data, [str(outside)])
        self.assertTrue((outside / 'keep').exists())

    def local_fixture(self):
        from test_remote_dev import app
        c = app.Controller(self.home, self.home / 'run')
        c.config_dir.mkdir(parents=True)
        (c.config_dir / 'auth.json').write_text('{"ownerToken":"fixture-only"}')
        runtime = c.data / 'runtimes/devspace/fixture'
        runtime.mkdir(parents=True)
        return c, runtime

    def test_repeat_install_and_uninstall_preserve_credentials_and_user_config(self):
        c, runtime = self.local_fixture()
        original = (c.config_dir / 'auth.json').read_bytes()
        def command(args, **kwargs):
            return 'PATH=/usr/bin:/bin\n'
        with patch.object(install, 'find_node', return_value=Path('/usr/bin/node')), \
                patch.object(install, 'install_runtime', return_value=runtime), \
                patch.object(install.subprocess, 'check_output', side_effect=command), \
                patch.object(c, 'systemctl'):
            install.local_install(c, ROOT)
            config = json.loads(c.config_file.read_text())
            config['workspaces']['allowedRoots'] = [str(self.home / 'custom')]
            c.config_file.write_text(json.dumps(config))
            install.local_install(c, ROOT)
            self.assertEqual(json.loads(c.config_file.read_text()), config)
            install.uninstall(c, ROOT)
        self.assertEqual((c.config_dir / 'auth.json').read_bytes(), original)
        self.assertEqual(json.loads(c.config_file.read_text()), config)
        self.assertFalse(runtime.exists())
        self.assertFalse(c.unit_file.exists())
        self.assertFalse((self.home / '.local/bin/remote-dev').exists())

    def test_install_after_uninstall_reuses_retained_config(self):
        c, runtime = self.local_fixture()
        with patch.object(install, 'find_node', return_value=Path('/usr/bin/node')), \
                patch.object(install, 'install_runtime', return_value=runtime), \
                patch.object(install.subprocess, 'check_output', return_value='PATH=/usr/bin\n'), \
                patch.object(c, 'systemctl'):
            install.local_install(c, ROOT)
            config = c.config_file.read_bytes()
            install.uninstall(c, ROOT)
            install.local_install(c, ROOT)
        self.assertEqual(c.config_file.read_bytes(), config)
        self.assertTrue((self.home/'.local/bin/remote-dev').is_symlink())

    def test_failed_unit_reload_restores_previous_configuration(self):
        c, runtime = self.local_fixture()
        c.config_file.write_text('{"workspaces":{"allowedRoots":["custom"]}}')
        record = c.data / 'installation.json'
        record.write_text('{"interface":2,"node":"/usr/bin/node"}')
        original = {p:p.read_bytes() for p in [c.config_file,record,c.config_dir/'auth.json']}
        with patch.object(install, 'find_node', return_value=Path('/usr/bin/node')), \
                patch.object(install, 'install_runtime', return_value=runtime), \
                patch.object(install.subprocess, 'check_output', return_value='PATH=/usr/bin\n'), \
                patch.object(c, 'systemctl', side_effect=[subprocess.CalledProcessError(1, ['systemctl']), None]):
            with self.assertRaises(subprocess.CalledProcessError):
                install.local_install(c, ROOT)
        for path, contents in original.items():
            self.assertEqual(path.read_bytes(),contents)
        self.assertFalse(c.unit_file.exists())

    def test_uninstall_preserves_modified_remote_registration_and_runtime(self):
        data=self.home/'.local/share/remote-dev'
        data.mkdir(parents=True)
        runtime=data/'runtimes/chrome-devtools/fixture'
        runtime.mkdir(parents=True)
        expected={'type':'stdio','command':'managed'}
        changed={'type':'stdio','command':'user-replacement'}
        (data/'installation.json').write_text(json.dumps({'managedBy':'remote-dev','owned_paths':[str(runtime)],'registrations':{'local-chrome':expected}}))
        (self.home/'.claude.json').write_text(json.dumps({'mcpServers':{'local-chrome':changed}}))
        with patch.object(install,'claude_command',return_value='/unused'):
            install.remote_uninstall(self.home,data)
        self.assertEqual(install.mcp_config(self.home)['local-chrome'],changed)
        self.assertTrue(runtime.exists())


if __name__ == '__main__':
    unittest.main()

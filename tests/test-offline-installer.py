#!/usr/bin/python3
"""Offline installer disk selection, password handling and cleanup regressions."""
import importlib.util
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


password = load('password', ROOT / 'scripts/lib/install-password.py')
installer = load('iso_installer', ROOT / 'scripts/iso/installer.py')


class OfflineInstaller(unittest.TestCase):
    @unittest.skipUnless(shutil.which('sshd'), 'requires OpenSSH server configuration parser')
    def test_ssh_password_enabled_only_for_provisioned_core(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            snippets = root / 'etc/ssh/sshd_config.d'
            snippets.mkdir(parents=True)
            default = snippets / '10-dcore.conf'
            default.write_text('PasswordAuthentication no\n')
            config = root / 'sshd_config'
            key = root / 'host-key'
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
            config.write_text(f'Include {snippets}/*.conf\nHostKey {key}\nPermitRootLogin prohibit-password\n')

            def effective(user):
                result = subprocess.check_output([shutil.which('sshd'), '-T', '-f', str(config),
                    '-C', f'user={user},host=test,addr=127.0.0.1'], text=True)
                return dict(line.split(' ', 1) for line in result.splitlines())

            self.assertEqual(effective('core')['passwordauthentication'], 'no')
            password.enable_core_password_ssh(root)
            self.assertEqual(effective('core')['passwordauthentication'], 'yes')
            self.assertEqual(effective('root')['passwordauthentication'], 'no')
            self.assertEqual(effective('another')['passwordauthentication'], 'no')
            self.assertIn(effective('root')['permitrootlogin'], ('prohibit-password', 'without-password'))
            self.assertEqual(default.read_text(), 'PasswordAuthentication no\n')

    def test_bad_password_never_wipes(self):
        for value in (b'', b'a\nb', b'a\rb', b'a\0b', b'\xff', b'x' * 4097):
            with mock.patch.object(password.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    password.install('/dev/test', [], value)
                run.assert_not_called()

    def test_password_written_to_deployment_through_stdin(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'mounts'
            path.mkdir()
            with mock.patch.object(password.tempfile, 'mkdtemp', return_value=str(path)), \
                 mock.patch.object(password, 'root_partition', return_value='/dev/test3'), \
                 mock.patch.object(password, 'enable_core_password_ssh') as configure_ssh, \
                 mock.patch.object(password.subprocess, 'run') as run:
                password.install('/dev/test', ['--skip-fetch-check'], b'example-secret')
                configure_ssh.assert_called_once_with(path / 'deployment')
            calls = run.call_args_list
            self.assertEqual(calls[0].args[0][:6], ['bootc', 'install', 'to-disk', '/dev/test', '--wipe', '--skip-fetch-check'])
            change = next(c for c in calls if c.args[0][0] == 'chpasswd')
            self.assertEqual(change.kwargs['input'], b'core:example-secret\n')
            self.assertEqual(change.args[0], ['chpasswd', '--root', str(path / 'deployment'), '--crypt-method', 'YESCRYPT'])
            self.assertFalse(any('example-secret' in str(c.args) for c in calls))
            self.assertEqual([c.args[0][0] for c in calls[-2:]], ['umount', 'umount'])
            self.assertFalse(path.exists())

    def test_password_failure_still_unmounts(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'mounts'
            path.mkdir()
            def execute(command, **kwargs):
                if command[0] == 'chpasswd':
                    raise subprocess.CalledProcessError(1, command)
            with mock.patch.object(password.tempfile, 'mkdtemp', return_value=str(path)), \
                 mock.patch.object(password, 'root_partition', return_value='/dev/test3'), \
                 mock.patch.object(password, 'enable_core_password_ssh') as configure_ssh, \
                 mock.patch.object(password.subprocess, 'run', side_effect=execute) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    password.install('/dev/test', [], b'example-secret')
                self.assertEqual([c.args[0][0] for c in run.call_args_list[-2:]], ['umount', 'umount'])
                configure_ssh.assert_not_called()

    def test_media_busy_small_and_mapped_disks_excluded(self):
        disks = [dict(name=name, type=kind, size=size) for name, kind, size in (
            ('/dev/media', 'disk', 32 * 1024**3), ('/dev/busy', 'disk', 32 * 1024**3),
            ('/dev/small', 'disk', 1024**3), ('/dev/mapped', 'lvm', 32 * 1024**3),
            ('/dev/good', 'disk', 32 * 1024**3))]
        checker = mock.Mock()
        checker.check.side_effect = [ValueError('mounted'), {'diskseq': 7}]
        with mock.patch.object(installer.subprocess, 'check_output', return_value=json.dumps({'blockdevices': disks})):
            result = installer.eligible_disks(checker, {'/dev/media'})
        self.assertEqual([disk['name'] for disk, _ in result], ['/dev/good'])
        self.assertEqual(checker.check.call_args_list, [mock.call('/dev/busy'), mock.call('/dev/good')])

    def test_unknown_media_fails_closed(self):
        with mock.patch.object(installer.subprocess, 'check_output', return_value=''):
            with self.assertRaisesRegex(ValueError, 'installer medium'):
                installer.media_devices()

    def test_media_parent_disk_excluded(self):
        with mock.patch.object(installer.subprocess, 'check_output', side_effect=['/dev/sda1\n', '/dev/sda1\n/dev/sda\n']), \
             mock.patch.object(installer.Path, 'is_block_device', return_value=True):
            self.assertEqual(installer.media_devices(), {'/dev/sda1', '/dev/sda'})

    def run_console(self, identity=None, confirmation='ERASE /dev/test', passwords=None):
        disk = dict(name='/dev/test', size=20 * 1024**3)
        checker = mock.Mock()
        checker.check.return_value = identity or {'diskseq': 1}
        with tempfile.TemporaryDirectory() as work:
            medium = Path(work)
            (medium / 'dcore').mkdir()
            (medium / 'dcore/installer.json').write_text(json.dumps(dict(
                release='test', debian_release='trixie', local_image='localhost/dcore:test',
                tracking_image='ghcr.io/rosti-cz/dcore:trixie')))
            with mock.patch.object(installer, 'MEDIUM', medium), \
                 mock.patch.object(installer.Path, 'is_dir', return_value=True), \
                 mock.patch.object(installer, 'module', side_effect=[checker, password]), \
                 mock.patch.object(installer, 'media_devices', return_value={'/dev/media'}), \
                 mock.patch.object(installer, 'eligible_disks', return_value=[(disk, {'diskseq': 1})]), \
                 mock.patch.object(installer.getpass, 'getpass', side_effect=passwords or ['test-password'] * 2), \
                 mock.patch('builtins.input', side_effect=['1', confirmation, '']), \
                 mock.patch.object(installer.subprocess, 'run') as run:
                try:
                    installer.install()
                except ValueError as error:
                    return run.call_args_list, error
                return run.call_args_list, None

    def test_console_offline_dispatch(self):
        calls, error = self.run_console()
        self.assertIsNone(error)
        self.assertEqual(calls[0].args[0][:2], ['podman', 'load'])
        command = calls[1].args[0]
        self.assertIn('--local', command)
        self.assertIn('--core-password-stdin', command)
        self.assertEqual(calls[1].kwargs['input'], b'test-password')
        self.assertNotIn('test-password', str(command))
        self.assertEqual(len(calls), 2)  # No reboot unless explicitly requested.

    def test_replaced_disk_never_reaches_block_installer(self):
        calls, error = self.run_console(identity={'diskseq': 2})
        self.assertRegex(str(error), 'Destination changed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0][:2], ['podman', 'load'])

    def test_password_mismatch_and_erasure_refusal_do_not_load_or_install(self):
        calls, error = self.run_console(passwords=['one', 'two'])
        self.assertRegex(str(error), 'Passwords do not match')
        self.assertEqual(calls, [])
        calls, error = self.run_console(confirmation='no')
        self.assertIsNone(error)
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)

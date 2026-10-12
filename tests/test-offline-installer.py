#!/usr/bin/python3
"""Offline installer disk selection, password handling and cleanup regressions."""
import importlib.util
import io
import json
import os
import types
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


class ResourceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.staging = self.root / 'staging'
        self.staging.mkdir()
        self.memory = self.root / 'meminfo'
        self.set_memory(16, 14)
        self.config = {'minimum_ram_bytes': 16 * installer.GIB, 'image_size_bytes': 4 * installer.GIB}
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def set_memory(self, total, available):
        self.memory.write_text(f'MemTotal: {int(total * 1024**2)} kB\nMemAvailable: {int(available * 1024**2)} kB\n')

    def check(self, free=True):
        installer.check_resources(self.config, require_free=free, staging=self.staging, meminfo=self.memory)

    def test_800_mib_refused_before_any_staging_operations(self):
        self.set_memory(0.8, 0.5)
        with mock.patch.object(installer.subprocess, 'check_output') as command:
            with self.assertRaisesRegex(ValueError, '16 GiB RAM; detected 0.8 GiB'):
                self.check()
            command.assert_not_called()
        self.assertFalse((self.staging / 'image-tmp').exists())

    def test_temporary_images_use_same_ram_storage_as_graphroot(self):
        with mock.patch.object(installer.subprocess, 'check_output', return_value='tmpfs\n'), \
             mock.patch.object(installer.os, 'statvfs', return_value=types.SimpleNamespace(f_bavail=12, f_frsize=installer.GIB)):
            self.check()
        temporary = self.staging / 'image-tmp'
        self.assertEqual(os.environ['TMPDIR'], str(temporary))
        self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)

    def test_kernel_memory_reservations_allow_nominal_16_gib_vm(self):
        self.set_memory(15.6, 14)
        with mock.patch.object(installer.subprocess, 'check_output', return_value='tmpfs\n'):
            self.check(free=False)

    def test_overlay_staging_is_refused(self):
        with mock.patch.object(installer.subprocess, 'check_output', return_value='overlay\n'):
            with self.assertRaisesRegex(ValueError, 'dedicated RAM filesystem'):
                self.check()

    def test_full_staging_or_low_free_memory_is_reported_before_import(self):
        with mock.patch.object(installer.subprocess, 'check_output', return_value='tmpfs\n'), \
             mock.patch.object(installer.os, 'statvfs', return_value=types.SimpleNamespace(f_bavail=2, f_frsize=installer.GIB)):
            with self.assertRaisesRegex(ValueError, 'free RAM staging space'):
                self.check()
        self.set_memory(16, 2)
        with mock.patch.object(installer.subprocess, 'check_output', return_value='tmpfs\n'), \
             mock.patch.object(installer.os, 'statvfs', return_value=types.SimpleNamespace(f_bavail=12, f_frsize=installer.GIB)):
            with self.assertRaisesRegex(ValueError, 'available RAM 2.0 GiB'):
                self.check()


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

    def run_console(self, identity=None, confirmation='ERASE /dev/test', passwords=None,
                    filesystem='ext4', encrypt=False, fs_choice=None, encryption_choice=None):
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
                 mock.patch.object(installer, 'check_encryption_support'), \
                 mock.patch.object(installer, 'check_resources'), \
                 mock.patch.object(installer.getpass, 'getpass',
                     side_effect=passwords or (['test-password'] * 2 + (['test-disk-passphrase'] * 2 if encrypt else []))), \
                 mock.patch('builtins.input', side_effect=['1', fs_choice if fs_choice is not None else str(('ext4', 'btrfs', 'xfs').index(filesystem) + 1),
                     encryption_choice if encryption_choice is not None else ('2' if encrypt else '1'), confirmation, '']), \
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

    def test_all_filesystems_with_and_without_encryption(self):
        for filesystem in ('ext4', 'btrfs', 'xfs'):
            for encrypt in (False, True):
                with self.subTest(filesystem=filesystem, encrypt=encrypt):
                    calls, error = self.run_console(filesystem=filesystem, encrypt=encrypt)
                    self.assertIsNone(error)
                    command = calls[1].args[0]
                    self.assertEqual(command[command.index('--filesystem') + 1], filesystem)
                    self.assertEqual('--encrypt' in command, encrypt)
                    if encrypt:
                        core, luks = password.read_credentials(io.BytesIO(calls[1].kwargs['input']))
                        self.assertEqual((core, luks), (b'test-password', b'test-disk-passphrase'))
                    else:
                        self.assertEqual(calls[1].kwargs['input'], b'test-password')
                    self.assertNotIn('test-password', str(command))
                    self.assertNotIn('test-disk-passphrase', str(command))

    def test_invalid_and_cancelled_choices_never_install(self):
        for choice in ('9', 'zfs', 'q'):
            calls, error = self.run_console(fs_choice=choice)
            self.assertEqual(calls, [])
            if choice != 'q':
                self.assertIsInstance(error, ValueError)
        calls, error = self.run_console(encryption_choice='q')
        self.assertEqual(calls, [])
        self.assertIsNone(error)
        calls, error = self.run_console(encryption_choice='3')
        self.assertEqual(calls, [])
        self.assertIsInstance(error, ValueError)

    def test_default_choices(self):
        calls, error = self.run_console(fs_choice='', encryption_choice='')
        self.assertIsNone(error)
        command = calls[1].args[0]
        self.assertEqual(command[command.index('--filesystem') + 1], 'ext4')
        self.assertNotIn('--encrypt', command)

    def test_encryption_passphrase_mismatch_never_loads_or_installs(self):
        calls, error = self.run_console(encrypt=True, passwords=['core', 'core', 'disk-one', 'disk-two'])
        self.assertRegex(str(error), 'Disk passphrases do not match')
        self.assertEqual(calls, [])

    def test_missing_live_encryption_driver_is_reported(self):
        with mock.patch.object(installer.shutil, 'which', return_value='/usr/bin/tool'), \
             mock.patch.object(installer.Path, 'is_file', return_value=True), \
             mock.patch.object(installer.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(ValueError, 'live kernel'):
                installer.check_encryption_support()

    def test_missing_mapper_rules_and_tools_fail_before_driver_load(self):
        with mock.patch.object(installer.subprocess, 'run') as run:
            with mock.patch.object(installer.shutil, 'which', return_value=None):
                with self.assertRaisesRegex(ValueError, 'Missing live encryption tool'):
                    installer.check_encryption_support()
            with mock.patch.object(installer.shutil, 'which', return_value='/usr/bin/tool'), \
                 mock.patch.object(installer.Path, 'is_file', return_value=False):
                with self.assertRaisesRegex(ValueError, 'udev notification rules'):
                    installer.check_encryption_support()
            run.assert_not_called()

    def test_secret_bundle_is_bounded_validated_and_redacted(self):
        core = ('λ:' + 'x' * 100).encode()
        luks = b'quote"backslash\\colon:password'
        self.assertEqual(password.read_credentials(io.BytesIO(password.encode_credentials(core, luks))), (core, luks))
        invalid = [b'', b'not-json-secret', b'x' * 65537,
                   b'{"version":1,"core_password":"secret","core_password":"secret","luks_passphrase":"secret"}']
        for data in ({'version': 2, 'core_password': 'secret', 'luks_passphrase': 'secret'},
                     {'version': True, 'core_password': 'secret', 'luks_passphrase': 'secret'},
                     {'version': 1, 'core_password': None, 'luks_passphrase': 'secret'},
                     {'version': 1, 'core_password': '', 'luks_passphrase': 'secret'},
                     {'version': 1, 'core_password': 'secret', 'luks_passphrase': 'a\nsecret'},
                     {'version': 1, 'core_password': 'secret'},
                     {'version': 1, 'core_password': 'secret', 'luks_passphrase': 'secret', 'extra': 'secret'}):
            invalid.append(json.dumps(data).encode())
        for raw in invalid:
            with self.subTest(raw_length=len(raw)):
                with self.assertRaises(ValueError) as raised:
                    password.read_credentials(io.BytesIO(raw))
                self.assertNotIn('secret', str(raised.exception))

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

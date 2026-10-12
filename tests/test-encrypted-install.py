#!/usr/bin/python3
"""Passphrase validation and encryption dispatch safety on a private loop disk."""
import argparse
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('encrypted_install', ROOT / 'scripts/lib/install-encrypted.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class PassphraseTests(unittest.TestCase):
    def test_console_compatible_input(self):
        self.assertEqual(helper.validate_passphrase('test λ passphrase'.encode()), 'test λ passphrase'.encode())
        for invalid in (b'', b'a\nb', b'a\rb', b'a\0b', b'\xff', b'a' * 4097):
            with self.assertRaises(ValueError):
                helper.validate_passphrase(invalid)

    def test_file_security_and_optional_newline(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'password'
            path.write_bytes(b'test-only-password\r\n')
            path.chmod(0o600)
            self.assertEqual(helper.read_passphrase_file(path), b'test-only-password')
            for mode in (0o644, 0o700):
                path.chmod(mode)
                with self.assertRaisesRegex(ValueError, 'mode 0600'):
                    helper.read_passphrase_file(path)
            path.chmod(0o600)
            symlink = Path(work) / 'link'
            symlink.symlink_to(path)
            with self.assertRaises(OSError):
                helper.read_passphrase_file(symlink)
            pipe = Path(work) / 'pipe'
            os.mkfifo(pipe, 0o600)
            with self.assertRaisesRegex(ValueError, 'regular file'):
                helper.read_passphrase_file(pipe)
            path.write_bytes(b'one\ntwo\n')
            with self.assertRaisesRegex(ValueError, 'single line'):
                helper.read_passphrase_file(path)

    def test_double_prompt_and_noninteractive_refusal(self):
        with mock.patch.object(helper.sys.stdin, 'isatty', return_value=True), \
             mock.patch.object(helper.getpass, 'getpass', side_effect=['same', 'same']) as prompt:
            self.assertEqual(helper.prompt_passphrase(), b'same')
            self.assertEqual(prompt.call_count, 2)
        with mock.patch.object(helper.sys.stdin, 'isatty', return_value=True), \
             mock.patch.object(helper.getpass, 'getpass', side_effect=['one', 'two']):
            with self.assertRaisesRegex(ValueError, 'do not match'):
                helper.prompt_passphrase()
        with mock.patch.object(helper.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'terminal'):
                helper.prompt_passphrase()


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest('run with sudo; dispatch tests allocate their own loop device')
        # Detach/rebind tests must not race another suite allocating loop devices.
        cls.loop_lock = os.open('/run/lock/dcore-loop-tests.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(cls.loop_lock, fcntl.LOCK_EX)
        cls.work = Path(tempfile.mkdtemp(prefix='dcore-encryption-tests-'))
        cls.raw = cls.work / 'disk.raw'
        with cls.raw.open('wb') as stream:
            stream.truncate(10 * 1024**3)
        cls.loop = subprocess.check_output(['losetup', '--find', '--show', str(cls.raw)], text=True).strip()
        cls.password = cls.work / 'password'
        cls.password.write_bytes(b'dcore-test-only-passphrase\n')
        cls.password.chmod(0o600)
        check_spec = importlib.util.spec_from_file_location('block_check', ROOT / 'scripts/lib/check-block-device.py')
        cls.checker = importlib.util.module_from_spec(check_spec)
        check_spec.loader.exec_module(cls.checker)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(['losetup', '--detach', cls.loop], check=True)
        shutil.rmtree(cls.work)
        os.close(cls.loop_lock)

    def args(self, filesystem='ext4'):
        return argparse.Namespace(device=self.loop, requested_device=self.loop,
                                  identity=json.dumps(self.checker.check(self.loop)),
                                  filesystem=filesystem, no_grow_root=False, image='ghcr.io/rosti-cz/dcore:trixie',
                                  ssh_key=str(self.work / 'public.pub'), passphrase_file=str(self.password))

    def test_secret_only_in_stdin_and_all_filesystems(self):
        for filesystem in ('ext4', 'btrfs', 'xfs'):
            args = self.args(filesystem)
            with mock.patch.object(helper, 'run') as invoke:
                helper.host_install(args)
            command = invoke.call_args.args[0]
            self.assertEqual(invoke.call_args.kwargs, {'input': b'dcore-test-only-passphrase'})
            self.assertNotIn('dcore-test-only-passphrase', ' '.join(command))
            self.assertNotIn(str(self.password), command)
            self.assertNotIn('--passphrase-file', command)
            self.assertEqual(command[command.index('--filesystem') + 1], filesystem)
            self.assertIn('-i', command)
            self.assertIn('--inside', command)

    def test_core_password_offline_dispatch_without_ssh_key(self):
        for filesystem in ('ext4', 'btrfs', 'xfs'):
            args = self.args(filesystem)
            args.ssh_key = None
            args.passphrase_file = None
            args.core_password_stdin = True
            args.local = True
            args.target_image = 'ghcr.io/rosti-cz/dcore:trixie'
            payload = helper.password_tools.encode_credentials(b'core-test-password', b'luks-test-password')
            with mock.patch.object(helper.sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(payload))), \
                 mock.patch.object(helper, 'run') as invoke:
                helper.host_install(args)
            command = invoke.call_args.args[0]
            self.assertIn('--network=none', command)
            self.assertIn('--core-password-stdin', command)
            self.assertNotIn('--ssh-key', command)
            self.assertEqual(command[command.index('--filesystem') + 1], filesystem)
            self.assertEqual(invoke.call_args.kwargs['input'], payload)
            self.assertNotIn('core-test-password', str(command))
            self.assertNotIn('luks-test-password', str(command))

    def test_malformed_credentials_never_launch_container(self):
        args = self.args()
        args.passphrase_file = None
        args.core_password_stdin = True
        with mock.patch.object(helper.sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(b'bad-secret-json'))), \
             mock.patch.object(helper, 'run') as invoke:
            with self.assertRaises(ValueError) as raised:
                helper.host_install(args)
            self.assertNotIn('bad-secret', str(raised.exception))
            invoke.assert_not_called()

    def test_growth_opt_out_forwarded(self):
        args = self.args()
        args.no_grow_root = True
        with mock.patch.object(helper, 'run') as invoke:
            helper.host_install(args)
        self.assertIn('--no-grow-root', invoke.call_args.args[0])

    def test_device_replacement_after_password_entry(self):
        args = self.args()
        # Simulate a changed kernel diskseq after prompting. Real detach/rebind
        # already has coverage in test-installers.py and can race udev claims.
        changed = json.loads(args.identity)
        changed['diskseq'] += 1
        checker = types.SimpleNamespace(check=lambda device: changed)
        with mock.patch.object(helper.importlib.util, 'module_from_spec', return_value=checker), \
             mock.patch.object(helper.importlib.util, 'spec_from_file_location') as spec_factory, \
             mock.patch.object(helper, 'run') as invoke:
            spec_factory.return_value.loader.exec_module.return_value = None
            with self.assertRaisesRegex(ValueError, 'identity or size changed'):
                helper.host_install(args)
            invoke.assert_not_called()

    def test_device_path_changed_after_password_entry(self):
        args = self.args()
        args.requested_device = str(self.work / 'missing-disk-link')
        with mock.patch.object(helper, 'run') as invoke:
            with self.assertRaisesRegex(ValueError, 'path changed'):
                helper.host_install(args)
            invoke.assert_not_called()


class InsideInstallTests(unittest.TestCase):
    def execute(self, filesystem='ext4', payload=None, fail_password=False, defer=False, fail_cleanup=False):
        with tempfile.TemporaryDirectory() as work:
            original_mkdtemp = tempfile.mkdtemp
            calls = []
            def execute(command, **kwargs):
                calls.append((command, kwargs))
                if command[:3] == ['bootc', 'install', 'mount']:
                    (Path(command[-1]) / 'etc').mkdir()
                if fail_password and command[0] == 'chpasswd':
                    raise subprocess.CalledProcessError(1, command)
                return types.SimpleNamespace(returncode=1 if fail_cleanup and command[:2] == ['cryptsetup', 'close'] else 0)
            def output(command, **kwargs):
                return {'blockdev': str(10 * 1024**3), 'blkid': 'efi-test-uuid', 'cryptsetup': 'luks-test-uuid'}[command[0]]
            args = argparse.Namespace(device='/dev/test', filesystem=filesystem, image='localhost/dcore:test',
                ssh_key=None, core_password_stdin=True, local=True, no_grow_root=False,
                target_image='ghcr.io/rosti-cz/dcore:trixie', defer_account=defer, state_dir=Path(work)/'state')
            data = payload if payload is not None else helper.password_tools.encode_credentials(b'core-fixture', b'luks-fixture')
            with mock.patch.object(helper.os, 'geteuid', return_value=0), \
                 mock.patch.object(helper.Path, 'is_block_device', return_value=True), \
                 mock.patch.object(helper, 'preflight'), \
                 mock.patch.object(helper.shutil, 'which', side_effect=lambda name: name), \
                 mock.patch.object(helper.subprocess, 'check_output', side_effect=output), \
                 mock.patch.object(helper.subprocess, 'run', side_effect=execute), \
                 mock.patch.object(helper.sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(data))), \
                 mock.patch.object(helper, 'partitions', return_value=('/dev/test2', '/dev/test3')), \
                 mock.patch.object(helper.tempfile, 'mkdtemp', side_effect=lambda prefix, dir: original_mkdtemp(prefix=prefix, dir=work)), \
                 mock.patch.object(helper.password_tools, 'write_layout', side_effect=lambda directory, data: calls.append((['checkpoint', json.loads(json.dumps(data))], {}))):
                error = None
                try:
                    helper.inside_install(args)
                except (ValueError, subprocess.CalledProcessError) as raised:
                    error = raised
            return calls, error

    def test_all_filesystems_provision_password_inside_encryption(self):
        for filesystem in ('ext4', 'btrfs', 'xfs'):
            with self.subTest(filesystem=filesystem):
                calls, error = self.execute(filesystem)
                self.assertIsNone(error)
                commands = [command for command, _ in calls]
                install = next(c for c in commands if c[:3] == ['bootc', 'install', 'to-filesystem'])
                self.assertIn('--skip-fetch-check', install)
                self.assertNotIn('--root-ssh-authorized-keys', install)
                self.assertEqual('--allow-missing-verity' in install, filesystem == 'xfs')
                self.assertTrue(any(c[0] == 'mkfs.' + filesystem for c in commands))
                change, params = next(c for c in calls if c[0][0] == 'chpasswd')
                self.assertEqual(params['input'], b'core:core-fixture\n')
                self.assertTrue(any(c[:2] == ['cryptsetup', 'close'] for c in commands))
                self.assertFalse(any('core-fixture' in str(c) or 'luks-fixture' in str(c) for c in commands))

    def test_deferred_account_records_completed_cleanup_without_configuring_password(self):
        for filesystem in ('ext4', 'btrfs', 'xfs'):
            calls, error = self.execute(filesystem, defer=True)
            self.assertIsNone(error)
            commands = [command for command, _ in calls]
            self.assertFalse(any(command[0] == 'chpasswd' for command in commands))
            checkpoints = [command[1] for command in commands if command[0] == 'checkpoint']
            self.assertEqual([item['clean'] for item in checkpoints], [False, True])
            self.assertTrue(all(item['deployed'] for item in checkpoints))

    def test_deferred_mapping_close_failure_keeps_cleanup_pending(self):
        calls, error = self.execute(defer=True, fail_cleanup=True)
        self.assertIsInstance(error, ValueError)
        checkpoints = [command[1] for command, _ in calls if command[0] == 'checkpoint']
        self.assertEqual([item['clean'] for item in checkpoints], [False])

    def test_bad_payload_never_wipes(self):
        calls, error = self.execute(payload=b'bad-secret-json')
        self.assertIsInstance(error, ValueError)
        self.assertFalse(any(c[0][0] == 'wipefs' for c in calls))
        self.assertNotIn('bad-secret', str(error))

    def test_password_failure_unmounts_and_closes_mapping(self):
        calls, error = self.execute(fail_password=True)
        self.assertIsInstance(error, subprocess.CalledProcessError)
        commands = [command for command, _ in calls]
        self.assertEqual(sum(c[0] == 'umount' for c in commands), 3)
        self.assertEqual(commands[-1][:2], ['cryptsetup', 'close'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

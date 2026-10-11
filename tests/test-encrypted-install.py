#!/usr/bin/python3
"""Passphrase validation and encryption dispatch safety on a private loop disk."""
import argparse
import fcntl
import importlib.util
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


if __name__ == '__main__':
    unittest.main(verbosity=2)

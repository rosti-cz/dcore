#!/usr/bin/python3
"""Secure Boot chain preservation, build-key validation and enrollment guards."""
import importlib.machinery
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]

def load(name, path):
    module = types.ModuleType(name)
    importlib.machinery.SourceFileLoader(name, str(path)).exec_module(module)
    return module

boot = load('boot_chain', ROOT / 'rootfs/usr/libexec/dcore/bootctl-shim')
enrollment = load('enrollment', ROOT / 'rootfs/usr/libexec/dcore/enroll-secureboot')
signing = load('signing', ROOT / 'scripts/sign-secureboot.py')

class BootChainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.esp = self.root/'boot'
        self.assets = {
            'usr/lib/shim/shimx64.efi.signed': b'trusted-shim',
            'usr/lib/shim/mmx64.efi.signed': b'trusted-mok-manager',
            'usr/lib/systemd/boot/efi/systemd-bootx64.efi.signed': b'trusted-systemd-boot',
            'usr/share/dcore/secureboot/dcore.der': b'public-certificate'}
        for name, value in self.assets.items():
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)

    def test_both_entry_paths_use_shim_and_correct_second_stage(self):
        boot.install_chain(self.root, self.esp, 'x86_64')
        for directory, filename in [('BOOT', 'BOOTX64.EFI'), ('systemd', 'systemd-bootx64.efi')]:
            path = self.esp/'EFI'/directory
            self.assertEqual((path/filename).read_bytes(), b'trusted-shim')
            self.assertEqual((path/'grubx64.efi').read_bytes(), b'trusted-systemd-boot')
            self.assertEqual((path/'mmx64.efi').read_bytes(), b'trusted-mok-manager')
        self.assertEqual((self.esp/'EFI/dcore/dcore.der').read_bytes(), b'public-certificate')

    def test_missing_component_leaves_existing_chain_untouched(self):
        boot.install_chain(self.root, self.esp, 'x86_64')
        (self.root/'usr/lib/shim/mmx64.efi.signed').unlink()
        with self.assertRaises(ValueError): boot.install_chain(self.root, self.esp, 'x86_64')
        self.assertEqual((self.esp/'EFI/BOOT/BOOTX64.EFI').read_bytes(), b'trusted-shim')

    def test_update_preserves_shim_without_native_bootctl_overwrite(self):
        boot.install_chain(self.root, self.esp, 'x86_64')
        with mock.patch.object(boot.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(boot.subprocess, 'check_output', return_value='rw,nosuid,nodev\n'), \
             mock.patch.object(boot.subprocess, 'run') as run:
            self.assertEqual(boot.main(['--root='+str(self.root), '--esp-path=/boot', 'update']), 0)
        run.assert_not_called()
        self.assertEqual((self.esp/'EFI/BOOT/BOOTX64.EFI').read_bytes(), b'trusted-shim')

    def test_readonly_mount_restored_even_if_update_fails(self):
        boot.install_chain(self.root, self.esp, 'x86_64')
        with mock.patch.object(boot.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(boot.subprocess, 'check_output', return_value='ro,nosuid\n'), \
             mock.patch.object(boot.subprocess, 'run') as run, \
             mock.patch.object(boot, 'install_chain', side_effect=OSError('injected write failure')):
            with self.assertRaises(OSError): boot.main(['--root', str(self.root), '--esp-path', '/boot', 'update'])
        self.assertEqual(run.call_args_list, [mock.call(['mount', '-o', 'remount,rw', str(self.esp)], check=True),
            mock.call(['mount', '-o', 'remount,ro', str(self.esp)], check=True)])

    def test_generic_install_preserves_no_variables_and_random_seed_flags(self):
        args = ['install', '--root', str(self.root), '--esp-path', '/boot', '--no-variables', '--random-seed', 'no']
        with mock.patch.object(boot.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(boot.subprocess, 'check_output', return_value='rw\n'), \
             mock.patch.object(boot.subprocess, 'run') as run:
            boot.main(args)
        run.assert_called_once_with([boot.REAL_BOOTCTL, *args], check=True)

    def test_missing_asset_refuses_before_native_install(self):
        (self.root/'usr/lib/shim/shimx64.efi.signed').unlink()
        with mock.patch.object(boot.platform, 'machine', return_value='x86_64'), \
             mock.patch.object(boot.subprocess, 'run') as run:
            with self.assertRaises(ValueError): boot.main(['install', '--root', str(self.root), '--esp-path', '/boot'])
        run.assert_not_called()

    def test_read_command_delegates_unchanged(self):
        with mock.patch.object(boot.os, 'execv', side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit): boot.main(['status', '--json=short'])
        execute.assert_called_once_with(boot.REAL_BOOTCTL, [boot.REAL_BOOTCTL, 'status', '--json=short'])

class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.object(enrollment.os, 'geteuid', return_value=0),
                      mock.patch.object(enrollment.sys.stdin, 'isatty', return_value=True),
                      mock.patch.object(enrollment.Path, 'is_dir', return_value=True),
                      mock.patch.object(enrollment.Path, 'is_file', return_value=True),
                      mock.patch.dict(os.environ)):
            patch.start(); self.addCleanup(patch.stop)

    def test_cancel_never_requests_enrollment(self):
        with mock.patch.object(enrollment.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='key is not enrolled', stderr='')) as run, \
             mock.patch('builtins.input', return_value='n'):
            enrollment.enroll()
        self.assertFalse(any('--import' in call.args[0] for call in run.call_args_list))

    def test_already_enrolled_is_idempotent(self):
        with mock.patch.object(enrollment.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='key is already enrolled', stderr='')) as run:
            enrollment.enroll()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ['mokutil', '--test-key', str(enrollment.CERTIFICATE)])
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_accepted_enrollment_uses_native_interactive_password_prompt(self):
        with mock.patch.object(enrollment.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='key is not enrolled', stderr='')) as run, \
             mock.patch('builtins.input', return_value='yes'):
            enrollment.enroll()
        self.assertEqual(run.call_args.args[0], ['mokutil', '--import', str(enrollment.CERTIFICATE)])
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_pending_request_and_blocked_key_never_import_again(self):
        for response in ('key is already in the enrollment request', 'key is blocked in dbx'):
            with mock.patch.object(enrollment.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=response, stderr='')) as run:
                if 'blocked' in response:
                    with self.assertRaises(ValueError): enrollment.enroll()
                else:
                    enrollment.enroll()
                self.assertEqual(run.call_count, 1)

    def test_noninteractive_and_non_uefi_refuse_before_commands(self):
        for patch in (mock.patch.object(enrollment.sys.stdin, 'isatty', return_value=False),
                      mock.patch.object(enrollment.Path, 'is_dir', return_value=False)):
            with patch, mock.patch.object(enrollment.subprocess, 'run') as run:
                with self.assertRaises(ValueError): enrollment.enroll()
                run.assert_not_called()

class BuildSigningTests(unittest.TestCase):
    def test_runner_secret_is_private_and_invalid_key_is_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work/'secureboot').mkdir()
            runner = work/'runner'; runner.mkdir()
            key = work/'fixture.key'
            subprocess.run(['openssl', 'req', '-new', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-subj', '/CN=Ephemeral test signing key', '-keyout', str(key),
                '-out', str(work/'secureboot/dcore.crt')], check=True, capture_output=True)
            environment = dict(os.environ, RUNNER_TEMP=str(runner), SECUREBOOT_SIGNING_KEY=key.read_text())
            script = ROOT/'scripts/prepare-secureboot-secret.py'
            subprocess.run([sys.executable, str(script)], cwd=work, env=environment, check=True, capture_output=True)
            secret = runner/'dcore-secureboot.key'
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            self.assertEqual(secret.read_bytes(), key.read_bytes())
            secret.unlink()
            environment['SECUREBOOT_SIGNING_KEY'] = 'invalid test key'
            failed = subprocess.run([sys.executable, str(script)], cwd=work, env=environment, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(secret.exists())
            self.assertNotIn(b'invalid test key', failed.stdout + failed.stderr)

    def test_missing_release_key_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(signing, 'CERTIFICATE', ROOT/'secureboot/dcore.crt'), \
             mock.patch.object(sys, 'argv', ['signing', '--key', str(Path(temp)/'missing')]):
            with self.assertRaisesRegex(ValueError, 'required'): signing.main()

    def test_mismatched_key_rejected_before_kernel_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp)/'fixture.key'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                            '-out', str(key)], check=True, capture_output=True)
            with mock.patch.object(signing, 'CERTIFICATE', ROOT/'secureboot/dcore.crt'), \
                 mock.patch.object(sys, 'argv', ['signing', '--key', str(key)]):
                with self.assertRaisesRegex(ValueError, 'does not match'): signing.main()

if __name__ == '__main__': unittest.main(verbosity=2)

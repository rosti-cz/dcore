#!/usr/bin/python3
"""Admin command confirmation, key preservation and target selection tests."""
import importlib.machinery
import json
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
admin = types.ModuleType('admin')
importlib.machinery.SourceFileLoader('admin', str(ROOT / 'rootfs/usr/libexec/dcore/admin')).exec_module(admin)


def result(stdout='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='')


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        for name, path in (('CORE_SSH', self.work/'core/.ssh'), ('ROOT_SSH', self.work/'root/.ssh'),
                           ('TIMER_DROPIN', self.work/'timer/20-ujust.conf')):
            patch = mock.patch.object(admin, name, path)
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(admin.sys.stdin, 'isatty', return_value=True)
        patch.start(); self.addCleanup(patch.stop)

    def test_existing_private_key_is_untouched(self):
        admin.CORE_SSH.mkdir(parents=True)
        key = admin.CORE_SSH/'id_ed25519'
        key.write_text('existing-private-test-fixture')
        original = key.stat()
        with mock.patch.object(admin.pwd, 'getpwnam'), mock.patch.object(admin, 'run') as run:
            admin.gen_sshkey()
        self.assertEqual(key.read_text(), 'existing-private-test-fixture')
        self.assertEqual(key.stat().st_mtime_ns, original.st_mtime_ns)
        run.assert_not_called()

    def test_key_generation_runs_as_core(self):
        account = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with mock.patch.object(admin.pwd, 'getpwnam', return_value=account), mock.patch.object(admin, 'run') as run:
            admin.gen_sshkey()
        command = run.call_args.args
        self.assertEqual(command[:5], ('runuser', '-u', 'core', '--', 'ssh-keygen'))
        self.assertIn('ed25519', command)
        self.assertEqual(admin.CORE_SSH.stat().st_mode & 0o777, 0o700)

    def test_password_change_is_interactive_and_keeps_tokens(self):
        with mock.patch.object(admin, 'root_luks', return_value='/dev/test-root'), mock.patch.object(admin, 'run') as run:
            admin.change_luks_password()
        self.assertEqual(run.call_args.args, ('cryptsetup', 'luksChangeKey', '--verify-passphrase', '/dev/test-root'))

    def test_tpm_confirmation_and_only_tpm_replacement(self):
        metadata = {'keyslots': {'0': {}, '1': {}}, 'tokens': {'0': {'type': 'systemd-tpm2', 'keyslots': ['1']}}}
        for answer in ('n', 'y'):
            with mock.patch.object(admin, 'root_luks', return_value='/dev/test-root'), \
                 mock.patch.object(admin.Path, 'exists', return_value=True), \
                 mock.patch('builtins.input', return_value=answer), \
                 mock.patch.object(admin, 'run', side_effect=[result('sha256:\n  7: 0x'+'a'*64), result(json.dumps(metadata)), result()]) as run:
                admin.enroll_tpm()
            calls = [c.args for c in run.call_args_list]
            if answer == 'n': self.assertEqual(len(calls), 2)
            else:
                command = calls[-1]
                self.assertEqual(command[0], 'systemd-cryptenroll')
                self.assertIn('--wipe-slot=tpm2', command)
                self.assertNotIn('--wipe-slot=all', command)
                self.assertNotIn('--wipe-slot=password', command)
                self.assertNotIn('--unlock-key-file', ' '.join(command))

    def test_tpm_rejects_unmeasured_firmware(self):
        with mock.patch.object(admin, 'root_luks', return_value='/dev/test-root'), \
             mock.patch.object(admin.Path, 'exists', return_value=True), \
             mock.patch.object(admin, 'run', return_value=result('sha256: 7: 0x'+'0'*64)) as run:
            with self.assertRaisesRegex(ValueError, 'unenforced'): admin.enroll_tpm()
        self.assertEqual(run.call_count, 1)

    def test_plain_root_password_and_tpm_are_noops(self):
        with mock.patch.object(admin, 'root_luks', return_value=None), mock.patch.object(admin, 'run') as run:
            admin.change_luks_password(); admin.enroll_tpm()
        run.assert_not_called()

    def test_schedule_refusal_has_no_mutations(self):
        with mock.patch.object(admin, 'booted_image'), mock.patch('builtins.input', side_effect=['1','03:15','n']), \
             mock.patch.object(admin, 'run', return_value=result()) as run:
            admin.enable_autoupgrade()
        self.assertFalse(admin.TIMER_DROPIN.exists())
        self.assertTrue(all(call.args[0] == 'systemd-analyze' for call in run.call_args_list))

    def test_schedule_acceptance_writes_real_newlines(self):
        with mock.patch.object(admin, 'booted_image'), mock.patch('builtins.input', side_effect=['6','02:30','yes']), \
             mock.patch.object(admin, 'run', return_value=result()) as run:
            admin.enable_autoupgrade()
        contents = admin.TIMER_DROPIN.read_text()
        self.assertEqual(contents, '[Timer]\nOnCalendar=\nOnCalendar=Sat *-*-* 02:30:00\nRandomizedDelaySec=0\n')
        self.assertIn(mock.call('systemctl','enable','--now','dcore-update.timer'), run.call_args_list)

    def test_invalid_time_and_noninteractive_refuse(self):
        with mock.patch.object(admin, 'booted_image'), mock.patch('builtins.input', side_effect=['1','25:00']), \
             mock.patch.object(admin, 'run') as run:
            with self.assertRaises(ValueError): admin.enable_autoupgrade()
        run.assert_not_called()
        with mock.patch.object(admin.sys.stdin, 'isatty', return_value=False):
            with self.assertRaises(ValueError): admin.enable_incus()

    def test_incus_toggle_and_refusal(self):
        for enabled, answer in ((False,'y'), (True,'y'), (True,'n'), (False,'n')):
            def invoke(*args, **kwargs):
                if args[1] == 'is-enabled': return result('enabled' if enabled else 'disabled')
                if args[1] == 'is-active': return result(returncode=0 if enabled else 3)
                return result()
            with mock.patch.object(admin, 'run', side_effect=invoke) as run, mock.patch('builtins.input', return_value=answer):
                admin.enable_incus()
            mutations = [call.args for call in run.call_args_list if call.args[1] not in ('is-enabled','is-active')]
            if answer == 'n': self.assertEqual(mutations, [])
            elif enabled: self.assertEqual(mutations, [('systemctl','disable','--now',*admin.INCUS_UNITS)])
            else: self.assertIn(('systemctl','start','incus.service'), mutations)
            self.assertFalse(any('incus-agent.service' in command for command in mutations))

    def test_upgrade_stages_without_apply(self):
        with mock.patch.object(admin, 'booted_image'), mock.patch.object(admin, 'run') as run:
            admin.upgrade()
        run.assert_called_once_with('bootc','upgrade')

    def test_public_key_rejects_private_content(self):
        key = self.work/'key.pub'; key.write_text('-----BEGIN OPENSSH PRIVATE KEY-----\nfixture\n')
        with mock.patch.object(admin, 'run') as run:
            self.assertFalse(admin.public_key(key))
        run.assert_not_called()

    def test_install_preserves_identity_and_invokes_guarded_installer(self):
        identity = {'diskseq': 1, 'size': 20*1024**3}
        checker = types.SimpleNamespace(check=mock.Mock(return_value=identity))
        devices = {'blockdevices':[{'name':'/dev/test-disk','type':'disk','size':20*1024**3,'model':'test','serial':'test'}]}
        with mock.patch.object(admin, 'booted_image', return_value='ghcr.io/rosti-cz/dcore:trixie'), \
             mock.patch.object(admin, 'load_module', return_value=checker), \
             mock.patch.object(admin, 'select_key', return_value=Path('/test/key.pub')), \
             mock.patch('builtins.input', side_effect=['1','1','y']), \
             mock.patch.object(admin, 'run', return_value=result(json.dumps(devices))) as run:
            admin.install()
        args = run.call_args.args
        self.assertEqual(args[:2], (str(admin.INSTALLER), '/dev/test-disk'))
        self.assertIn('--encrypt', args)
        self.assertNotIn('--confirm-device', args)
        self.assertEqual(checker.check.call_count, 2)

    def test_install_cancels_on_device_replacement(self):
        checker = types.SimpleNamespace(check=mock.Mock(side_effect=[{'diskseq':1}, {'diskseq':2}]))
        devices={'blockdevices':[{'name':'/dev/test-disk','type':'disk','size':20*1024**3}]}
        with mock.patch.object(admin, 'booted_image', return_value='example:trixie'), \
             mock.patch.object(admin, 'load_module', return_value=checker), \
             mock.patch.object(admin, 'select_key', return_value=Path('/test/key.pub')), \
             mock.patch('builtins.input', side_effect=['1','1','n']), \
             mock.patch.object(admin, 'run', return_value=result(json.dumps(devices))) as run:
            with self.assertRaisesRegex(ValueError, 'identity changed'): admin.install()
        self.assertEqual(run.call_count, 1)

    def test_booted_image_schema(self):
        data={'status':{'booted':{'image':{'image':{'image':'ghcr.io/rosti-cz/dcore:trixie','transport':'registry'}}}}}
        with mock.patch.object(admin, 'run', return_value=result(json.dumps(data))):
            self.assertEqual(admin.booted_image(), 'ghcr.io/rosti-cz/dcore:trixie')


if __name__ == '__main__': unittest.main(verbosity=2)

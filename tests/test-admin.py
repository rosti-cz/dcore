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
privileged = types.ModuleType('privileged')
importlib.machinery.SourceFileLoader('privileged', str(ROOT / 'rootfs/usr/libexec/dcore/admin-privileged')).exec_module(privileged)
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
        with mock.patch.object(admin.pwd, 'getpwnam'), mock.patch.object(admin, 'run', return_value=result('{"exists":true}')) as run:
            admin.gen_sshkey()
        self.assertEqual(key.read_text(), 'existing-private-test-fixture')
        self.assertEqual(key.stat().st_mtime_ns, original.st_mtime_ns)
        run.assert_called_once_with(admin.PRIVILEGED_HELPER, 'core-key-status', privileged=True, capture=True)

    def test_key_generation_runs_as_core(self):
        account = types.SimpleNamespace(pw_uid=1000, pw_gid=1000)
        with mock.patch.object(admin.pwd, 'getpwnam', return_value=account), \
             mock.patch.object(admin.os, 'geteuid', return_value=1001), \
             mock.patch.object(admin, 'run', return_value=result('{"exists":false}')) as run:
            admin.gen_sshkey()
        command = run.call_args.args
        self.assertEqual(command[:6], ('/usr/bin/sudo', '-u', 'core', '-H', '--', 'ssh-keygen'))
        self.assertIn('ed25519', command)
        self.assertIn(mock.call(admin.PRIVILEGED_HELPER, 'prepare-core-ssh', privileged=True), run.call_args_list)

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

    def test_schedule_acceptance_delegates_privileged_write(self):
        with mock.patch.object(admin, 'booted_image'), mock.patch('builtins.input', side_effect=['6','02:30','yes']), \
             mock.patch.object(admin, 'run', return_value=result()) as run:
            admin.enable_autoupgrade()
        self.assertFalse(admin.TIMER_DROPIN.exists())
        self.assertIn(mock.call(admin.PRIVILEGED_HELPER, 'write-timer', 'Sat *-*-* 02:30:00', privileged=True), run.call_args_list)
        self.assertIn(mock.call('systemctl','enable','--now','dcore-update.timer', privileged=True), run.call_args_list)
        privileged.write_timer(admin.TIMER_DROPIN, 'Sat *-*-* 02:30:00')
        self.assertEqual(admin.TIMER_DROPIN.read_text(), '[Timer]\nOnCalendar=\nOnCalendar=Sat *-*-* 02:30:00\nRandomizedDelaySec=0\n')

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
            else:
                self.assertIn(('systemctl','start','incus.service'), mutations)
                self.assertIn(mock.call('groupadd','--force','--system','incus-admin',privileged=True),run.call_args_list)
                self.assertIn(mock.call('usermod','--append','--groups','incus-admin','core',privileged=True),run.call_args_list)
            self.assertFalse(any('incus-agent.service' in command for command in mutations))

    def test_locale_and_timezone_apply_only_selected_value_with_sudo(self):
        for action, tool, listing, setting, value in (
            (admin.switch_locale, 'localectl', 'list-locales', 'set-locale', 'cs_CZ.utf8'),
            (admin.set_timezone, 'timedatectl', 'list-timezones', 'set-timezone', 'Europe/Prague'),
        ):
            with self.subTest(tool=tool), mock.patch.object(admin, 'run',
                    side_effect=[result(value+'\n'), result(value+'\n'), result()]) as run:
                action()
            self.assertEqual(run.call_args_list[0], mock.call(tool, '--no-pager', listing, capture=True))
            self.assertEqual(run.call_args_list[1].args[0], 'fzf')
            self.assertNotIn('privileged', run.call_args_list[1].kwargs)
            self.assertEqual(run.call_args_list[-1], mock.call(tool, setting, value, privileged=True))

    def test_picker_cancellation_and_errors_never_apply_settings(self):
        for action in (admin.switch_locale, admin.set_timezone):
            for response in (result(returncode=1), result(returncode=130),
                             result('unknown\n'), result('en_US.utf8\ncs_CZ.utf8\n'), result(returncode=2)):
                with self.subTest(action=action.__name__, response=response), \
                     mock.patch.object(admin, 'run', side_effect=[result('en_US.utf8\n'), response]) as run:
                    if response.returncode in (1, 130):
                        action()
                    else:
                        with self.assertRaises((ValueError, subprocess.CalledProcessError)): action()
                self.assertEqual(run.call_count, 2)
                self.assertFalse(any(call.kwargs.get('privileged') for call in run.call_args_list))

    def test_picker_requires_terminal_and_available_options(self):
        for action in (admin.switch_locale, admin.set_timezone):
            with mock.patch.object(admin.sys.stdin, 'isatty', return_value=False), \
                 mock.patch.object(admin, 'run') as run:
                with self.assertRaises(ValueError): action()
                run.assert_not_called()
            with mock.patch.object(admin, 'run', return_value=result()) as run:
                with self.assertRaises(ValueError): action()
                self.assertEqual(run.call_count, 1)

    def test_picker_ignores_automatic_selection_defaults(self):
        with mock.patch.dict(os.environ, {'FZF_DEFAULT_OPTS': '--filter=cs_CZ',
                'FZF_DEFAULT_OPTS_FILE': '/custom/options', 'TERM': 'xterm'}), \
             mock.patch.object(admin, 'run', return_value=result('cs_CZ.utf8\n')) as run:
            self.assertEqual(admin.fuzzy_select('Locale', ['cs_CZ.utf8']), 'cs_CZ.utf8')
        self.assertNotIn('FZF_DEFAULT_OPTS', run.call_args.kwargs['env'])
        self.assertNotIn('FZF_DEFAULT_OPTS_FILE', run.call_args.kwargs['env'])
        self.assertEqual(run.call_args.kwargs['env']['TERM'], 'xterm')

    def test_failed_setting_is_reported_without_success_message(self):
        for action in (admin.switch_locale, admin.set_timezone):
            with mock.patch.object(admin, 'run', side_effect=[result('value\n'), result('value\n'),
                    subprocess.CalledProcessError(1, 'setting')]), mock.patch('builtins.print') as output:
                with self.assertRaises(subprocess.CalledProcessError): action()
            output.assert_not_called()

    def test_upgrade_stages_os_and_upgrades_brew_as_core(self):
        with mock.patch.object(admin, 'booted_image'), \
             mock.patch.object(admin.pwd, 'getpwnam', return_value=types.SimpleNamespace(pw_uid=1000)), \
             mock.patch.object(admin.os, 'geteuid', return_value=1000), mock.patch.object(admin, 'run') as run:
            admin.upgrade()
        self.assertEqual(run.call_args_list, [mock.call('bootc','upgrade',privileged=True),
            mock.call(admin.BREW,'update'),mock.call(admin.BREW,'upgrade')])
        self.assertFalse(any('--apply' in c.args for c in run.call_args_list))

    def test_upgrade_retries_only_failed_brew_step(self):
        calls=[]
        def invoke(*args, **kwargs):
            calls.append(args)
            if args==(admin.BREW,'update') and calls.count(args)==1:
                raise subprocess.CalledProcessError(1,args)
            return result()
        with mock.patch.object(admin,'booted_image'), \
             mock.patch.object(admin.pwd,'getpwnam',return_value=types.SimpleNamespace(pw_uid=1000)), \
             mock.patch.object(admin.os,'geteuid',return_value=1000), \
             mock.patch.object(admin,'run',side_effect=invoke),mock.patch('builtins.input',return_value='y'):
            admin.upgrade()
        self.assertEqual(calls.count(('bootc','upgrade')),1)
        self.assertEqual(calls.count((admin.BREW,'update')),2)
        self.assertEqual(calls[-1],(admin.BREW,'upgrade'))

    def test_homebrew_only_uses_owner_without_root_brew(self):
        with mock.patch.object(admin.pwd,'getpwnam',return_value=types.SimpleNamespace(pw_uid=1000)), \
             mock.patch.object(admin.os,'geteuid',return_value=1001),mock.patch.object(admin,'run') as run, \
             mock.patch.object(admin,'booted_image') as status:
            admin.upgrade('homebrew')
        status.assert_not_called()
        self.assertEqual(run.call_args_list,[mock.call('/usr/bin/sudo','-u','core','-H','--',admin.BREW,'update'),
                                            mock.call('/usr/bin/sudo','-u','core','-H','--',admin.BREW,'upgrade')])

    def test_root_entry_rejected_even_with_sudo_user(self):
        with mock.patch.object(admin.os,'geteuid',return_value=0), \
             mock.patch.dict(os.environ,{'SUDO_USER':'core','SUDO_UID':'1000'}), \
             mock.patch.object(admin.sys,'argv',['admin','upgrade']),mock.patch.object(admin,'upgrade') as upgrade:
            self.assertEqual(admin.main(),1)
        upgrade.assert_not_called()

    def test_privileged_commands_resolve_system_path(self):
        with mock.patch.object(admin.shutil,'which',return_value='/usr/bin/bootc') as which, \
             mock.patch.object(admin.subprocess,'run',return_value=result()) as run:
            admin.run('bootc','upgrade',privileged=True)
        which.assert_called_once_with('bootc',path='/usr/sbin:/usr/bin:/sbin:/bin')
        self.assertEqual(run.call_args.args[0],('/usr/bin/sudo','--','/usr/bin/bootc','upgrade'))

    def test_secureboot_enrollment_elevates_only_fixed_interactive_helper(self):
        with mock.patch.object(admin, 'run') as run:
            admin.enroll_secureboot()
        run.assert_called_once_with('/usr/libexec/dcore/enroll-secureboot', privileged=True)
        with mock.patch.object(admin.sys.stdin, 'isatty', return_value=False), mock.patch.object(admin, 'run') as run:
            with self.assertRaises(ValueError): admin.enroll_secureboot()
            run.assert_not_called()

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
             mock.patch.object(admin, 'check_disk', side_effect=checker.check), \
             mock.patch.object(admin, 'select_key', return_value=Path('/test/key.pub')), \
             mock.patch('builtins.input', side_effect=['1','1','y']), \
             mock.patch.object(admin, 'run', return_value=result(json.dumps(devices))) as run:
            admin.install('plain')
        args = run.call_args.args
        self.assertEqual(args[:2], (str(admin.INSTALLER), '/dev/test-disk'))
        self.assertIn('--encrypt', args)
        self.assertNotIn('--confirm-device', args)
        self.assertEqual(checker.check.call_count, 2)

    def test_install_cancels_on_device_replacement(self):
        checker = types.SimpleNamespace(check=mock.Mock(side_effect=[{'diskseq':1}, {'diskseq':2}]))
        devices={'blockdevices':[{'name':'/dev/test-disk','type':'disk','size':20*1024**3}]}
        with mock.patch.object(admin, 'booted_image', return_value='example:trixie'), \
             mock.patch.object(admin, 'check_disk', side_effect=checker.check), \
             mock.patch.object(admin, 'select_key', return_value=Path('/test/key.pub')), \
             mock.patch('builtins.input', side_effect=['1','1','n']), \
             mock.patch.object(admin, 'run', return_value=result(json.dumps(devices))) as run:
            with self.assertRaisesRegex(ValueError, 'identity changed'): admin.install('plain')
        self.assertEqual(run.call_count, 1)

    def test_install_starts_shared_wizard_without_root_frontend(self):
        with mock.patch.object(admin.os, 'execv') as execute, \
             mock.patch.object(admin, 'run') as command:
            admin.install()
        execute.assert_called_once_with('/usr/bin/python3', ['/usr/bin/python3', '/usr/libexec/dcore/installer/installer.py', '--host'])
        command.assert_not_called()

    def test_booted_image_schema(self):
        data={'status':{'booted':{'image':{'image':{'image':'ghcr.io/rosti-cz/dcore:trixie','transport':'registry'}}}}}
        with mock.patch.object(admin, 'run', return_value=result(json.dumps(data))):
            self.assertEqual(admin.booted_image(), 'ghcr.io/rosti-cz/dcore:trixie')


if __name__ == '__main__': unittest.main(verbosity=2)

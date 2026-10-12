#!/usr/bin/python3
"""Checkpoint recovery and retry safety without touching real disks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_session', ROOT / 'scripts/iso/session.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        (self.work / 'medium/dcore').mkdir(parents=True)
        (self.work / 'medium/dcore/installer.json').write_text(json.dumps({
            'local_image': 'localhost/dcore:test', 'tracking_image': 'example/dcore:trixie'}))
        self.session = self.open()
        self.session.settings('/dev/fixture', {'device': '/dev/fixture', 'diskseq': 42, 'size': 10 * 1024**3}, 'xfs', True)
        self.session.credentials(b'private-core-fixture', b'private-disk-fixture')

    def open(self):
        return module.InstallerSession(self.work / 'state', ROOT / 'scripts', self.work / 'medium')

    def tearDown(self):
        self.session.close()
        self.temporary.cleanup()

    def test_configure_retry_skips_completed_deployment(self):
        phases = []
        def run(phase, confirmation, log):
            phases.append(phase)
            if phase == 'configure' and phases.count('configure') == 1:
                raise ValueError('Injected account failure')
        with mock.patch.object(self.session, 'run_phase', side_effect=run):
            self.assertFalse(self.session.run('ERASE /dev/fixture'))
            self.assertEqual(self.session.state['failed'], 'configure')
            self.assertTrue(self.session.run())
        self.assertEqual(phases, ['load', 'validate', 'install', 'configure', 'configure', 'verify', 'cleanup'])

    def test_failure_after_os_checkpoint_recovers_cleanup_before_configuration(self):
        phases = []
        def run(phase, confirmation, log):
            phases.append(phase)
            if phase == 'install':
                (self.session.directory / 'layout.json').write_text(json.dumps({'deployed': True}))
                raise ValueError('Injected mapping close failure')
        with mock.patch.object(self.session, 'run_phase', side_effect=run), \
             mock.patch.object(self.session, 'finish') as finish:
            self.assertFalse(self.session.run('ERASE /dev/fixture'))
            self.assertEqual(self.session.state['failed'], 'cleanup')
            self.assertIn('install', self.session.state['completed'])
            self.assertTrue(self.session.run())
            finish.assert_called_once()
            self.assertEqual(finish.call_args.args[0], 'cleanup')
        self.assertEqual(phases.count('install'), 1)

    def test_restart_restores_checkpoints_but_never_credentials(self):
        self.session.state.update(completed=['load', 'validate', 'install'], running='configure')
        self.session.save()
        self.session.close()
        self.session = self.open()
        self.assertEqual(self.session.state['failed'], 'configure')
        self.assertEqual(self.session.state['completed'], ['load', 'validate', 'install'])
        self.assertIsNone(self.session.core_password)
        self.assertIsNone(self.session.luks_passphrase)
        with self.assertRaisesRegex(ValueError, 'Re-enter'):
            self.session.payload()
        checkpoint = (self.session.directory / 'session.json').read_text()
        self.assertNotIn('private-core-fixture', checkpoint)
        self.assertNotIn('private-disk-fixture', checkpoint)
        self.assertEqual((self.session.directory / 'session.json').stat().st_mode & 0o777, 0o600)

    def test_interrupted_configure_recovers_backend_checkpoint(self):
        self.session.state.update(completed=['load', 'validate', 'install'], running='configure')
        (self.session.directory / 'layout.json').write_text(json.dumps({'deployed': True, 'configured': True, 'clean': False}))
        self.session.save()
        self.session.close()
        self.session = self.open()
        self.assertIn('configure', self.session.state['completed'])
        self.assertEqual(self.session.state['failed'], 'cleanup')

    def test_low_ram_stops_before_loading_or_deploying(self):
        with mock.patch.object(self.session.checks, 'check_resources', side_effect=ValueError('Need 16 GiB RAM')), \
             mock.patch.object(self.session, 'command') as command:
            self.assertFalse(self.session.run('ERASE /dev/fixture'))
            self.assertEqual(self.session.state['failed'], 'load')
            self.assertEqual(self.session.state['completed'], [])
            command.assert_not_called()

    def test_second_interface_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Another installer'):
            self.open()

    def test_os_deployment_requires_fresh_confirmation(self):
        with mock.patch.object(self.session, 'command') as command, \
             mock.patch.object(self.session, 'validate_identity'):
            with self.assertRaisesRegex(ValueError, 'Fresh erase'):
                self.session.run_phase('install')
            command.assert_not_called()
            self.session.run_phase('install', 'ERASE /dev/fixture')
            args, payload, _ = command.call_args.args
            self.assertIn('--defer-account', args)
            self.assertIn('--encrypt', args)
            self.assertNotIn('private-core-fixture', str(args))
            self.assertEqual(json.loads(payload)['core_password'], 'private-core-fixture')

    def test_existing_deployment_cannot_be_erased_or_reconfigured(self):
        (self.session.directory / 'layout.json').write_text('{"deployed": true}')
        with self.assertRaisesRegex(ValueError, 'already deployed'):
            self.session.run_phase('install', 'ERASE /dev/fixture')
        with self.assertRaisesRegex(ValueError, 'already deployed'):
            self.session.settings('/dev/other', {}, 'ext4', False)

    def test_replaced_disk_is_rejected_before_configuration(self):
        with mock.patch.object(self.session.checker, 'check', return_value={'device': '/dev/fixture', 'diskseq': 43}), \
             mock.patch.object(self.session, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'identity or size changed'):
                self.session.finish('configure', lambda line: None)
            command.assert_not_called()

    def test_stdin_uses_memory_and_errors_do_not_echo_credentials(self):
        logs = []
        self.session.command([sys.executable, '-c', 'import sys; assert sys.stdin.buffer.read() == b"private-core-fixture"; print("OK")'],
                             b'private-core-fixture', logs.append)
        self.assertEqual(logs, ['OK'])
        with self.assertRaisesRegex(ValueError, 'exit status 7') as error:
            self.session.command([sys.executable, '-c', 'import sys; sys.stdin.read(); sys.exit(7)'], b'private-disk-fixture')
        self.assertNotIn('private-disk-fixture', str(error.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)

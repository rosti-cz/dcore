#!/usr/bin/python3
"""Exercise the real Textual app, including threaded failures and retries."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import subprocess
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/iso'))
from interface import InstallerApp, EraseConfirmation, LogView
from session import InstallerSession, STEPS
from textual.widgets import Input, Checkbox, Select, Button, Static


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        work = Path(self.temp.name)
        (work / 'medium/dcore').mkdir(parents=True)
        (work / 'medium/dcore/installer.json').write_text(json.dumps({'local_image': 'localhost/dcore:test'}))
        self.session = InstallerSession(work / 'state', ROOT / 'scripts', work / 'medium')
        self.disk = {'name': '/dev/fixture', 'size': 10 * 1024**3, 'model': 'Fixture', 'serial': '1234'}
        self.identity = {'device': '/dev/fixture', 'size': 10 * 1024**3, 'diskseq': 12}
        self.disks = mock.patch.object(self.session, 'disks', return_value=[(self.disk, self.identity)])
        self.disks.start()
        self.resources = mock.patch.object(self.session.checks, 'check_resources')
        self.resources_mock = self.resources.start()
        self.app = InstallerApp(self.session)

    def tearDown(self):
        self.disks.stop()
        self.resources.stop()
        self.session.close()
        self.temp.cleanup()

    async def test_insufficient_ram_blocks_review_before_installation(self):
        self.resources_mock.side_effect = ValueError('This offline installer needs at least 16 GiB RAM; detected 0.8 GiB.')
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(self.app.query_one('#review', Button).disabled)
            self.assertIn('16 GiB RAM', str(self.app.query_one('#form-error', Static).render()))
            self.assertEqual(self.session.state['completed'], [])

    def fill(self, encrypted=False):
        self.app.query_one('#core-password', Input).value = 'private-core-fixture'
        self.app.query_one('#core-confirm', Input).value = 'private-core-fixture'
        self.app.query_one('#filesystem', Select).value = 'xfs'
        if encrypted:
            self.app.query_one('#encrypt', Checkbox).value = True
            self.app.query_one('#disk-password', Input).value = 'private-disk-fixture'
            self.app.query_one('#disk-confirm', Input).value = 'private-disk-fixture'

    async def test_settings_and_exact_erase_confirmation_at_small_terminal(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.fill(True)
            self.app.review()
            await pilot.pause()
            self.assertIsInstance(self.app.screen, EraseConfirmation)
            self.app.screen.query_one('#erase', Input).value = 'ERASE /dev/other'
            self.app.screen.confirm()
            await pilot.pause()
            self.assertIsInstance(self.app.screen, EraseConfirmation)
            self.assertFalse(self.app.busy)
            self.app.screen.action_cancel()
            await pilot.pause()
            self.assertFalse(self.app.busy)
            self.assertEqual(self.session.state['settings']['filesystem'], 'xfs')
            self.assertTrue(self.session.state['settings']['encrypt'])
            for id in ('core-password', 'core-confirm', 'disk-password', 'disk-confirm'):
                self.assertTrue(self.app.query_one('#' + id, Input).password)

    async def test_quit_shortcuts_are_blocked_during_installation(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            with mock.patch.object(self.app, 'exit') as exit_app:
                self.app.busy = True
                await pilot.press('ctrl+q', 'ctrl+c')
                exit_app.assert_not_called()
                self.app.busy = False
                await pilot.press('ctrl+q')
                exit_app.assert_called_once()

    async def test_rescue_shell_clears_console_and_returns_to_progress(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            output = io.StringIO()
            with mock.patch.object(self.app, 'suspend', return_value=contextlib.nullcontext()), \
                 mock.patch('interface.subprocess.run') as shell, \
                 mock.patch('sys.stdout', output):
                self.app.shell()
            shell.assert_called_once_with(['/bin/bash'], check=False)
            self.assertTrue(output.getvalue().startswith('\033[2J\033[H'))
            self.assertIn('Type exit', output.getvalue())

    async def test_host_completion_keeps_reboot_unavailable(self):
        self.session.HOST_MODE = True
        with mock.patch.object(self.session, 'run_phase'):
            async with self.app.run_test() as pilot:
                await pilot.pause()
                self.fill()
                self.app.review()
                await pilot.pause()
                self.app.screen.query_one('#erase', Input).value = 'ERASE /dev/fixture'
                self.app.screen.confirm()
                await self.app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(self.app.query_one('#reboot').display)
                self.assertIn('selected drive', str(self.app.query_one('#status', Static).render()))
                with mock.patch('interface.subprocess.run') as reboot:
                    self.app.on_button_pressed(Button.Pressed(self.app.query_one('#reboot', Button)))
                    reboot.assert_not_called()

    async def test_mismatch_blocks_confirmation(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            self.fill()
            self.app.query_one('#core-confirm', Input).value = 'different'
            self.app.review()
            await pilot.pause()
            self.assertNotIsInstance(self.app.screen, EraseConfirmation)
            self.assertIn('do not match', str(self.app.query_one('#form-error', Static).render()))

    async def test_worker_failure_retry_logs_and_reboot_gate(self):
        phases = []
        def run(phase, confirmation, log):
            phases.append(phase)
            log('fixture output')
            if phase == 'install':
                (self.session.directory / 'layout.json').write_text('{"deployed":true,"clean":true}')
            if phase == 'configure' and phases.count(phase) == 1:
                raise ValueError('Injected configuration failure')
        with mock.patch.object(self.session, 'run_phase', side_effect=run):
            async with self.app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                self.fill()
                self.app.review()
                await pilot.pause()
                self.app.screen.query_one('#erase', Input).value = 'ERASE /dev/fixture'
                self.app.screen.confirm()
                await self.app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(self.app.busy)
                self.assertEqual(self.session.state['failed'], 'configure')
                self.assertTrue(self.app.query_one('#reboot', Button).disabled)
                self.assertFalse(self.app.query_one('#retry', Button).disabled)
                self.app.action_logs()
                await pilot.pause()
                self.assertIsInstance(self.app.screen, LogView)
                self.app.screen.action_close()
                await pilot.pause()
                self.app.action_retry()
                await self.app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(phases.count('install'), 1)
                self.assertEqual(self.session.state['completed'], [phase for phase, _ in STEPS])
                self.assertFalse(self.app.query_one('#reboot', Button).disabled)
                log = self.app.log_file.read_text()
                self.assertIn('fixture output', log)
                self.assertNotIn('private-core-fixture', log)

    async def test_saved_deployment_is_locked_and_resumes_without_erase_modal(self):
        self.session.settings(self.disk['name'], self.identity, 'btrfs', True)
        (self.session.directory / 'layout.json').write_text('{"deployed":true,"clean":true}')
        self.session.reconcile()
        with mock.patch.object(self.session, 'run_phase') as phase:
            async with self.app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                for id in ('disks', 'filesystem', 'encrypt', 'rescan'):
                    self.assertTrue(self.app.query_one('#' + id).disabled)
                for id in ('core-password', 'core-confirm'):
                    self.app.query_one('#' + id, Input).value = 'private-core-fixture'
                for id in ('disk-password', 'disk-confirm'):
                    self.app.query_one('#' + id, Input).value = 'private-disk-fixture'
                self.app.review()
                await self.app.workers.wait_for_complete()
                await pilot.pause()
                self.assertNotIsInstance(self.app.screen, EraseConfirmation)
                self.assertEqual([call.args[0] for call in phase.call_args_list], ['configure', 'verify', 'cleanup'])


    async def test_enrollment_failure_is_retryable_without_redeploying(self):
        async with self.app.run_test() as pilot:
            await pilot.pause()
            button = self.app.query_one('#enroll-key', Button)
            self.assertTrue(button.disabled)
            completed = [phase for phase, _ in STEPS]
            self.session.state['completed'] = completed.copy()
            self.app.finished(True)
            self.assertFalse(button.disabled)
            with mock.patch.object(self.app, 'suspend', return_value=contextlib.nullcontext()), \
                 mock.patch('interface.os.geteuid', return_value=1000), \
                 mock.patch('interface.subprocess.run', return_value=subprocess.CompletedProcess([], 1)) as run, \
                 mock.patch('builtins.input', return_value=''), mock.patch.object(self.session, 'run') as install:
                self.app.on_button_pressed(types.SimpleNamespace(button=button))
            run.assert_called_once_with(['/usr/bin/sudo', '--', '/usr/libexec/dcore/enroll-secureboot'])
            install.assert_not_called()
            self.assertEqual(self.session.state['completed'], completed)
            self.assertIn('retry', str(self.app.query_one('#status', Static).render()))


if __name__ == '__main__':
    unittest.main(verbosity=2)

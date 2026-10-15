#!/usr/bin/python3
"""A bad checker, interrupted recovery, or timer rerun must not loop reboots."""
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    'guard', str(ROOT / 'rootfs/usr/libexec/dcore/guarded-update'))
spec = importlib.util.spec_from_loader(loader.name, loader)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
REAL_HEALTH = guard.health
REAL_RUN = guard.run
health_loader = importlib.machinery.SourceFileLoader(
    'update_health', str(ROOT / 'rootfs/usr/libexec/dcore/update-health'))
health_spec = importlib.util.spec_from_loader(health_loader.name, health_loader)
checker = importlib.util.module_from_spec(health_spec)
health_spec.loader.exec_module(checker)


def entry(name, backend='composefs'):
    detail = {'verity': name, 'bootDigest': name} if backend == 'composefs' else {
        'checksum': name, 'deploySerial': 0, 'stateroot': 'default'}
    return {'image': {'imageDigest': 'sha256:' + name}, backend: detail}


class GuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enabled = self.root / 'enabled'
        self.enabled.touch()
        self.boot_id = self.root / 'boot_id'
        self.boot_id.write_text('new-boot')
        for name, value in (('STATE_DIR', self.root), ('ENABLED', self.enabled),
                            ('BOOT_ID', self.boot_id)):
            patch = mock.patch.object(guard, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.run = self.patch('run')
        self.status = self.patch('status')
        self.health = self.patch('health', return_value=False)
        self.patch('time', mock.Mock())
        self.state = {'version': 1, 'phase': 'pending', 'boot_id': 'old-boot',
                      'source': guard.identity(entry('old')), 'target': guard.identity(entry('new'))}
        self.current = {'booted': entry('new'), 'rollback': entry('old')}
        self.status.return_value = self.current
        guard.save(self.state)

    def patch(self, name, *args, **kwargs):
        patch = mock.patch.object(guard, name, *args, **kwargs)
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def main(self, command):
        with mock.patch.object(sys, 'argv', ['guarded-update', command]):
            guard.main()

    def assert_no_recovery(self):
        self.assertNotIn(mock.call('bootc', 'rollback'), self.run.call_args_list)
        self.assertNotIn(mock.call('systemctl', 'reboot'), self.run.call_args_list)

    def test_success_commits_without_reboot(self):
        self.health.return_value = True
        self.main('check')
        self.assertEqual(guard.load()['phase'], 'idle')
        self.assert_no_recovery()

    def test_transient_failure_recovers_without_rollback(self):
        self.health.side_effect = [False, False, True]
        self.main('check')
        self.assertEqual(guard.load()['phase'], 'idle')
        self.assert_no_recovery()

    def test_one_rollback_then_persistent_pause_even_when_both_images_fail(self):
        def action(*args, **kwargs):
            # Durable pause must exist BEFORE any recovery side effect.
            self.assertEqual(guard.load()['phase'], 'paused')
            if args == ('bootc', 'rollback'):
                self.current['rollbackQueued'] = True
        self.run.side_effect = action
        self.main('check')
        self.assertEqual(self.health.call_count, 3)
        self.assertEqual(self.run.call_args_list,
                         [mock.call('bootc', 'rollback'), mock.call('systemctl', 'reboot')])
        self.current.update(booted=entry('old'), rollback=entry('new'), rollbackQueued=False)
        for _ in range(3):
            self.main('check')
            with self.assertRaises(ValueError):
                self.main('update')
        self.assertEqual(self.run.call_count, 2)

    def test_checker_crash_timeout_and_invalid_status_pause_without_reboot(self):
        for error in (ValueError('checker crashed'), OSError('missing checker'),
                      subprocess.TimeoutExpired('checker', 30)):
            with self.subTest(error=error):
                guard.save(self.state)
                self.health.side_effect = error
                with self.assertRaises(type(error)):
                    self.main('check')
                self.assertEqual(guard.load()['phase'], 'paused')
                self.main('check')
                self.assert_no_recovery()

    def test_interrupted_check_or_stage_never_retries(self):
        for phase in ('checking', 'staging'):
            guard.save({**self.state, 'phase': phase})
            self.main('check')
            self.assertEqual(guard.load()['phase'], 'paused')
            self.assert_no_recovery()

    def test_same_boot_does_not_check(self):
        self.boot_id.write_text('old-boot')
        self.main('check')
        self.health.assert_not_called()
        self.assert_no_recovery()

    def test_changed_deployment_or_missing_rollback_never_reboots(self):
        for change in ({'booted': entry('other')}, {'rollback': None},
                       {'rollback': entry('other')}, {'staged': entry('other')},
                       {'rollbackQueued': True}):
            with self.subTest(change=change):
                guard.save(self.state)
                self.status.return_value = {**self.current, **change}
                with self.assertRaises(ValueError):
                    self.main('check')
                self.assertEqual(guard.load()['phase'], 'paused')
                self.assert_no_recovery()

    def test_rollback_error_or_no_queued_recovery_never_retries(self):
        for error in (subprocess.CalledProcessError(1, 'bootc'), None):
            guard.save(self.state)
            self.run.reset_mock(side_effect=True)
            self.run.side_effect = error
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                self.main('check')
            self.assertEqual(guard.load()['phase'], 'paused')
            self.main('check')
            self.assertEqual(self.run.call_args_list, [mock.call('bootc', 'rollback')])

    def test_failed_state_write_prevents_recovery(self):
        self.patch('save', side_effect=OSError('disk full'))
        with self.assertRaises(OSError):
            self.main('check')
        self.assert_no_recovery()

    def test_failed_durable_pause_after_health_failure_prevents_rollback(self):
        real_save = guard.save
        def save(state):
            if state['phase'] == 'paused':
                raise OSError('fsync failed')
            real_save(state)
        self.patch('save', side_effect=save)
        with self.assertRaises(OSError):
            self.main('check')
        self.assertEqual(guard.load()['phase'], 'checking')
        self.assertEqual(self.health.call_count, 3)
        self.assert_no_recovery()

    def test_corrupt_or_future_state_prevents_all_actions(self):
        for content in ('{', '{"version":2,"phase":"idle"}',
                        '{"version":1,"phase":"unknown"}'):
            (self.root / 'state.json').write_text(content)
            with self.assertRaises(ValueError):
                self.main('update')
            self.run.assert_not_called()

    def test_opt_out_does_not_bypass_pause(self):
        self.enabled.unlink()
        guard.save({'phase': 'paused'})
        with self.assertRaises(ValueError):
            self.main('update')
        self.run.assert_not_called()

    def test_opt_out_preserves_original_update_behavior_and_pull_retries(self):
        guard.save({'phase': 'idle'})
        self.enabled.unlink()
        self.main('update')
        self.run.assert_called_once_with('bootc', 'upgrade', '--apply', timeout=7000)
        self.run.side_effect = subprocess.CalledProcessError(1, 'bootc')
        with self.assertRaises(subprocess.CalledProcessError):
            self.main('update')
        self.assertEqual(guard.load()['phase'], 'idle')

    def test_disabling_after_stage_prevents_recovery(self):
        self.enabled.unlink()
        self.main('check')
        self.assertEqual(guard.load()['phase'], 'paused')
        self.assert_no_recovery()

    def test_operator_resume_requires_health_and_no_queued_change(self):
        guard.save({'phase': 'paused'})
        with self.assertRaises(ValueError):
            self.main('resume')
        self.assertEqual(guard.load()['phase'], 'paused')
        self.health.return_value = True
        self.main('resume')
        self.assertEqual(guard.load()['phase'], 'idle')
        self.assert_no_recovery()

    def test_resume_cannot_clear_pause_with_a_queued_change(self):
        self.health.return_value = True
        for change in ({'staged': entry('other')}, {'rollbackQueued': True}):
            guard.save({'phase': 'paused'})
            self.status.return_value = {**self.current, **change}
            with self.assertRaises(ValueError):
                self.main('resume')
            self.assertEqual(guard.load()['phase'], 'paused')

    def test_unparseable_bootc_status_pauses_without_recovery(self):
        self.status.side_effect = ValueError('invalid status JSON')
        with self.assertRaises(ValueError):
            self.main('check')
        self.assertEqual(guard.load()['phase'], 'paused')
        self.assert_no_recovery()

    def test_failed_upgrade_pauses_without_reboot(self):
        guard.save({'phase': 'idle'})
        self.health.return_value = True
        self.status.return_value = {'booted': entry('old')}
        self.run.side_effect = subprocess.CalledProcessError(1, 'bootc')
        with self.assertRaises(subprocess.CalledProcessError):
            self.main('update')
        self.assertEqual(guard.load()['phase'], 'paused')
        self.assert_no_recovery()

    def test_paused_reason_survives_repeated_timer_runs(self):
        guard.save({'phase': 'paused', 'reason': 'original incident'})
        with self.assertRaises(ValueError):
            self.main('update')
        self.main('check')
        self.assertEqual(guard.load()['reason'], 'original incident')

    def test_update_records_transaction_before_reboot_for_both_backends(self):
        for backend in ('ostree', 'composefs'):
            guard.save({'phase': 'idle'})
            self.health.return_value = True
            self.status.side_effect = [
                {'booted': entry('old', backend)},
                {'booted': entry('old', backend), 'staged': entry('new', backend)}]
            def action(*args, **kwargs):
                phase = 'pending' if args == ('systemctl', 'reboot') else 'staging'
                self.assertEqual(guard.load()['phase'], phase)
            self.run.side_effect = action
            self.main('update')
            self.assertEqual(guard.load()['target'], guard.identity(entry('new', backend)))

    def test_unchanged_image_does_not_reboot(self):
        guard.save({'phase': 'idle'})
        self.health.return_value = True
        self.status.return_value = {'booted': entry('old')}
        self.main('update')
        self.assertEqual(guard.load()['phase'], 'idle')
        self.run.assert_called_once_with('bootc', 'upgrade', timeout=7000)

    def test_preflight_failure_does_not_stage(self):
        guard.save({'phase': 'idle'})
        self.status.return_value = {'booted': entry('old')}
        self.main('update')
        self.run.assert_not_called()
        self.assertEqual(guard.load()['phase'], 'paused')

    def test_health_exit_contract(self):
        # Exercise the real checker wrapper, including non-1 error exits.
        with mock.patch.object(guard.subprocess, 'run') as process:
            for code in (0, 1, 2, 127, -11):
                process.return_value.returncode = code
                if code in (0, 1):
                    self.assertEqual(REAL_HEALTH(), code == 0)
                else:
                    with self.assertRaises(ValueError):
                        REAL_HEALTH()

    def test_privileged_commands_ignore_user_managed_path(self):
        with mock.patch.object(guard.subprocess, 'run') as process:
            REAL_RUN('bootc', 'rollback')
            process.assert_called_once_with(('/usr/bin/bootc', 'rollback'),
                                            check=True, timeout=120)


class DefaultHealthTests(unittest.TestCase):
    def test_active_local_services_pass(self):
        with mock.patch.object(checker.subprocess, 'run', return_value=
                               subprocess.CompletedProcess([], 0, 'active\n')) as process:
            self.assertEqual(checker.main(), 0)
            self.assertEqual(process.call_count, 2)

    def test_inactive_services_explicitly_fail(self):
        for state in ('inactive', 'failed', 'activating'):
            with mock.patch.object(checker.subprocess, 'run', return_value=
                                   subprocess.CompletedProcess([], 0, state)):
                self.assertEqual(checker.main(), 1)

    def test_systemctl_errors_and_unknown_states_are_checker_errors(self):
        for code, state in ((1, ''), (0, ''), (0, 'unknown')):
            with mock.patch.object(checker.subprocess, 'run', return_value=
                                   subprocess.CompletedProcess([], code, state)):
                self.assertEqual(checker.main(), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

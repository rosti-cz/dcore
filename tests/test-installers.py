#!/usr/bin/python3
"""Installer command/safety tests using private loop disks and a fake Podman."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


class Installers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest('run with sudo; block tests allocate their own loop device')
        # Detach/rebind tests must not race another suite allocating loop devices.
        cls.loop_lock = os.open('/run/lock/dcore-loop-tests.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(cls.loop_lock, fcntl.LOCK_EX)
        cls.work = Path(tempfile.mkdtemp(prefix='dcore-installer-tests-'))
        cls.bin = cls.work / 'bin'
        cls.bin.mkdir()
        cls.log = cls.work / 'calls.jsonl'
        cls.raw = cls.work / 'disk.raw'
        with cls.raw.open('wb') as stream:
            stream.truncate(1024**3)
        cls.loop = subprocess.check_output(['losetup', '--find', '--show', '--partscan', str(cls.raw)], text=True).strip()
        cls.mount = cls.work / 'mounted'
        cls.mount.mkdir()
        cls.key = cls.work / 'key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(cls.key)], check=True)
        for name, source in {
            'incus': '''import json,os,sys
with open(os.environ['DCORE_TEST_LOG'],'a') as out: out.write(json.dumps(["incus"]+sys.argv[1:])+"\\n")
if sys.argv[1:3] == ['list','--format=json'] or sys.argv[1:4] == ['storage','volume','list']:
 print('[]')
''',
            'jq': '''import json,sys
items=json.load(sys.stdin)
name=sys.argv[sys.argv.index('--arg')+2]
sys.exit(0 if any(x.get('name')==name for x in items) else 1)
''',
            'podman': '''import json,os,subprocess,sys
args=sys.argv[1:]
with open(os.environ['DCORE_TEST_LOG'],'a') as out: out.write(json.dumps(['podman']+args)+"\\n")
if args[0]=='pull':
 mode=os.environ.get('DCORE_TEST_PULL_MODE')
 if mode=='mount': subprocess.run(['mount',os.environ['DCORE_TEST_PARTITION'],os.environ['DCORE_TEST_MOUNT']],check=True)
 if mode=='replace':
  subprocess.run(['losetup','--detach',os.environ['DCORE_TEST_DEVICE']],check=True)
  subprocess.run(['losetup',os.environ['DCORE_TEST_DEVICE'],os.environ['DCORE_TEST_RAW']],check=True)
''',
        }.items():
            file = cls.bin / name
            file.write_text(f'#!{sys.executable}\n' + source)
            file.chmod(0o755)
        cls.env = dict(os.environ, PATH=f'{cls.bin}:{os.environ["PATH"]}',
                       DCORE_TEST_LOG=str(cls.log), DCORE_TEST_DEVICE=cls.loop,
                       DCORE_TEST_RAW=str(cls.raw), DCORE_TEST_MOUNT=str(cls.mount),
                       DCORE_TEST_PARTITION=cls.loop + 'p2')
        spec = importlib.util.spec_from_file_location('block_check', ROOT / 'scripts/lib/check-block-device.py')
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)

    @classmethod
    def tearDownClass(cls):
        # Never recursively remove a directory if a fixture is still mounted.
        mounted = subprocess.run(['mountpoint', '-q', str(cls.mount)]).returncode == 0
        if mounted:
            subprocess.run(['umount', str(cls.mount)], check=True)
        subprocess.run(['blockdev', '--setrw', cls.loop], check=True)
        subprocess.run(['losetup', '--detach', cls.loop], check=True)
        shutil.rmtree(cls.work)
        os.close(cls.loop_lock)

    def setUp(self):
        self.log.write_text('')

    def run_script(self, script, args, mode=None):
        env = dict(self.env)
        if mode:
            env['DCORE_TEST_PULL_MODE'] = mode
        return subprocess.run([str(ROOT / 'scripts' / script), *args], env=env,
                              text=True, capture_output=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def block_args(self):
        return [self.loop, '--ssh-key', str(self.key) + '.pub', '--confirm-device', self.loop]

    def test_incus_filesystems(self):
        for filesystem in (None, 'ext4', 'btrfs', 'xfs'):
            self.log.write_text('')
            args = ['dcore-test'] + (['--filesystem', filesystem] if filesystem else [])
            result = self.run_script('incus_install', args)
            self.assertEqual(result.returncode, 0, result.stderr)
            install = next(c for c in self.calls() if 'to-disk' in c)
            self.assertEqual(install[install.index('--filesystem') + 1], filesystem or 'ext4')
            self.assertEqual('--allow-missing-verity' in install, filesystem == 'xfs')
            self.assertEqual(install[install.index('to-disk') + 1], '/dev/disk/by-id/virtio-incus_target')

    def test_invalid_filesystems_and_release_conversion(self):
        for filesystem in ('zfs', 'btrfs', 'xfs'):
            self.log.write_text('')
            args = ['dcore-test', '--filesystem', filesystem]
            if filesystem != 'zfs': args += ['--release', 'v1.0.0']
            result = self.run_script('incus_install', args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.calls(), [])
        result = self.run_script('block_install', self.block_args() + ['--filesystem', 'zfs'])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_block_filesystems(self):
        for filesystem in (None, 'ext4', 'btrfs', 'xfs'):
            self.log.write_text('')
            args = self.block_args() + (['--filesystem', filesystem] if filesystem else [])
            result = self.run_script('block_install', args)
            self.assertEqual(result.returncode, 0, result.stderr)
            install = next(c for c in self.calls() if c[0:2] == ['podman', 'run'])
            self.assertEqual(install[install.index('--filesystem') + 1], filesystem or 'ext4')
            self.assertEqual('--allow-missing-verity' in install, filesystem == 'xfs')
            self.assertIn('--wipe', install)
            self.assertIn('--generic-image', install)
            self.assertEqual(install[install.index('to-disk') + 1], self.loop)

    def test_wrong_confirmation_and_private_key(self):
        args = self.block_args()
        args[-1] = '/dev/NOT_THE_TARGET'
        result = self.run_script('block_install', args)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        result = self.run_script('block_install', [self.loop, '--ssh-key', str(self.key), '--confirm-device', self.loop])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.key.read_text(), result.stderr)
        self.assertEqual(self.calls(), [])

    def test_read_only_and_regular_file(self):
        subprocess.run(['blockdev', '--setro', self.loop], check=True)
        try:
            result = self.run_script('block_install', self.block_args())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('read-only', result.stderr)
            self.assertEqual(self.calls(), [])
        finally:
            subprocess.run(['blockdev', '--setrw', self.loop], check=True)
        result = self.run_script('block_install', [str(self.raw), '--ssh-key', str(self.key) + '.pub', '--confirm-device', str(self.raw)])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_replacement_during_pull(self):
        result = self.run_script('block_install', self.block_args(), mode='replace')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('identity or size changed', result.stderr)
        self.assertFalse(any(c[:2] == ['podman', 'run'] for c in self.calls()))

    def test_busy_device_and_partition(self):
        subprocess.run(['sfdisk', self.loop], input='label: gpt\nsize=16M, type=linux\ntype=linux\n',
                       text=True, stdout=subprocess.DEVNULL, check=True)
        subprocess.run(['partx', '--update', self.loop], check=True)
        subprocess.run(['udevadm', 'settle'], check=True)
        partition = self.loop + 'p2'
        subprocess.run(['mkfs.ext4', '-q', partition], check=True)
        result = self.run_script('block_install', [partition, '--ssh-key', str(self.key) + '.pub', '--confirm-device', partition])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('partition', result.stderr)
        self.assertEqual(self.calls(), [])
        subprocess.run(['mount', partition, str(self.mount)], check=True)
        try:
            result = self.run_script('block_install', self.block_args())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('mounted', result.stderr)
            self.assertEqual(self.calls(), [])
        finally:
            subprocess.run(['umount', str(self.mount)], check=True)
        # Device becomes mounted while the mocked registry pull is running.
        result = self.run_script('block_install', self.block_args(), mode='mount')
        try:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('mounted', result.stderr)
            self.assertFalse(any(c[:2] == ['podman', 'run'] for c in self.calls()))
        finally:
            subprocess.run(['umount', str(self.mount)], check=True)

    def test_mount_hidden_in_another_namespace(self):
        partition = self.loop + 'p2'
        ready = self.work / 'namespace-ready'
        source = "import pathlib,signal,subprocess,sys; subprocess.run(['mount',sys.argv[1],sys.argv[2]],check=True); pathlib.Path(sys.argv[3]).touch(); signal.pause()"
        process = subprocess.Popen(['unshare', '--mount', '--propagation', 'private',
                                    sys.executable, '-c', source, partition, str(self.mount), str(ready)])
        try:
            import time
            for _ in range(100):
                if ready.exists(): break
                time.sleep(0.05)
            self.assertTrue(ready.exists(), 'private mount was not ready')
            result = self.run_script('block_install', self.block_args())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('busy', result.stderr)
            self.assertEqual(self.calls(), [])
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_foreign_mount_namespace(self):
        with mock.patch.object(self.checker.os, 'readlink', side_effect=['self', 'other']):
            with self.assertRaisesRegex(ValueError, 'mount namespace'):
                self.checker.check(self.loop)


if __name__ == '__main__':
    unittest.main(verbosity=2)

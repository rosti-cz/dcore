#!/usr/bin/python3
"""Exercise the Incus installer lifecycle without a daemon or privileged disks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NAME = 'dcore-test'
VOLUME = NAME + '-installer-root'
HELPER = NAME + '-installer'

FAKE_INCUS = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
state_path = pathlib.Path(os.environ['DCORE_TEST_STATE'])
state = json.loads(state_path.read_text())
call = ['incus'] + args
if args[:2] == ['config', 'edit']:
    edited = json.load(sys.stdin)
    call.append(edited)
with open(os.environ['DCORE_TEST_LOG'], 'a') as log:
    log.write(json.dumps(call) + '\n')

def fail(message):
    sys.exit(message)

def save():
    state_path.write_text(json.dumps(state))

def expanded(instance):
    devices = {'root': {'type': 'disk', 'path': '/', 'pool': 'default'},
               'eth0': {'type': 'nic', 'network': 'incusbr0'}}
    if os.environ.get('DCORE_TEST_PROFILE_OS'):
        devices['os'] = {'type': 'disk', 'source': '/some/path'}
    devices.update(instance['devices'])
    return devices

fault = os.environ.get('DCORE_TEST_FAIL')
if args[:1] == ['list']:
    result = []
    for name, instance in state['instances'].items():
        result.append(dict(instance, name=name, expanded_devices=expanded(instance)))
    print(json.dumps(result))
elif args[:3] == ['storage', 'volume', 'list']:
    print(json.dumps([{'name': name, 'type': 'custom'} for name in state['volumes']]))
elif args[:2] == ['profile', 'show']:
    pass
elif args[:1] == ['init']:
    name = args[1] if '--empty' in args else args[2]
    pool = args[args.index('-s') + 1]
    size = next(arg.split('=', 1)[1] for arg in args if arg.startswith('root,size='))
    state['instances'][name] = {
        'architecture': 'x86_64', 'config': {'limits.cpu': '2'},
        'devices': {'root': {'type': 'disk', 'path': '/', 'pool': pool, 'size': size}},
        'ephemeral': False, 'profiles': [args[args.index('-p') + 1]],
        'description': '', 'running': False, 'starts': 0,
    }
    save()
elif args[:1] == ['start']:
    instance = state['instances'][args[1]]
    if fault == 'boot' and instance['starts'] == 1:
        fail('Injected final boot failure')
    devices = expanded(instance)
    managed = [d for d in devices.values() if d.get('type') == 'disk' and d.get('path') == '/']
    assert len(managed) == 1 and 'source' not in managed[0]
    root = devices.get('root', {})
    if root.get('source'):
        assert state['volumes'][root['source']]['prepared']
        assert int(root['boot.priority']) > int(devices['os']['boot.priority'])
        instance['phase'] = 'installer'
    elif instance['devices'].get('os', {}).get('path') == '/':
        assert instance.get('installed')
        instance['phase'] = 'dcore'
    instance['running'] = True
    instance['starts'] += 1
    save()
elif args[:1] == ['stop']:
    state['instances'][args[1]]['running'] = False
    save()
elif args[:3] == ['storage', 'volume', 'create']:
    state['volumes'][args[4]] = {'prepared': False}
    save()
elif args[:3] == ['storage', 'volume', 'delete']:
    if fault == 'delete-volume':
        fail('Injected volume deletion failure')
    for instance in state['instances'].values():
        assert all(d.get('source') != args[4] for d in expanded(instance).values())
    del state['volumes'][args[4]]
    save()
elif args[:3] == ['config', 'device', 'add']:
    instance = state['instances'][args[3]]
    instance['devices'][args[4]] = dict(type=args[5], **dict(arg.split('=', 1) for arg in args[6:]))
    save()
elif args[:3] == ['config', 'device', 'remove']:
    instance = state['instances'][args[3]]
    assert not instance['running']
    del instance['devices'][args[4]]
    save()
elif args[:2] == ['config', 'get']:
    print('a' * 64)
elif args[:2] == ['image', 'export']:
    if fault != 'export':
        pathlib.Path(args[3] + '.root').write_bytes(b'clean Debian image')
elif args[:2] == ['config', 'edit']:
    if fault == 'edit':
        fail('Injected configuration failure')
    instance = state['instances'][args[2]]
    assert not instance['running']
    assert edited['profiles'] == instance['profiles']
    instance.update(edited)
    save()
elif args[:1] == ['exec']:
    instance = state['instances'][args[1]]
    assert instance['running']
    command = args[3:]
    if command[:2] == ['qemu-img', 'convert']:
        if fault == 'convert':
            fail('Injected conversion failure')
        assert command[-2:] == ['/root/debian.qcow2', '/dev/disk/by-id/virtio-incus_target']
        volume = instance['devices']['target']['source']
        state['volumes'][volume]['prepared'] = True
        save()
    elif 'to-disk' in command:
        if fault == 'install':
            fail('Injected installation failure')
        assert instance['phase'] == 'installer'
        assert command[command.index('to-disk') + 1] == '/dev/disk/by-id/virtio-incus_os'
        assert instance['devices']['os']['path'] == '/'
        assert 'source' not in instance['devices']['os']
        instance['installed'] = True
        save()
    elif command == ['bootc', 'status']:
        if fault == 'status':
            fail('Injected status failure')
        if 'os' in instance['devices']:
            assert instance['phase'] == 'dcore'
elif args[:1] == ['delete']:
    del state['instances'][args[1]]
    save()
elif args[:2] in (['file', 'push'], ['console', 'dcore-test'], ['image', 'import'], ['image', 'alias']):
    if args[:3] == ['image', 'alias', 'list']:
        print('[]')
elif args[:1] == ['console']:
    pass
else:
    fail('Unexpected Incus command: ' + repr(args))
'''


class IncusInstaller(unittest.TestCase):
    def setUp(self):
        if not shutil.which('jq'):
            self.skipTest('requires jq')
        self.directory = tempfile.TemporaryDirectory(prefix='dcore-incus-tests-')
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.bin = self.work / 'bin'
        self.bin.mkdir()
        self.log = self.work / 'calls.jsonl'
        self.log.touch()
        self.state_path = self.work / 'state.json'
        self.state_path.write_text(json.dumps({'instances': {}, 'volumes': {}}))
        self.write_command('incus', FAKE_INCUS)
        # Release downloads are deterministic fixtures verified by real sha256sum.
        self.write_command('curl', '''import hashlib,pathlib,sys
args=sys.argv[1:]
path=pathlib.Path(args[args.index('-o')+1])
data=b'release fixture'
if path.name == 'SHA256SUMS':
 path.write_text(hashlib.sha256(data).hexdigest()+'  dcore-v1.0.0-trixie-amd64-incus.tar.xz\\n')
else: path.write_bytes(data)
''')
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        DCORE_TEST_LOG=str(self.log), DCORE_TEST_STATE=str(self.state_path))

    def write_command(self, name, source):
        path = self.bin / name
        path.write_text(f'#!{sys.executable}\n' + source)
        path.chmod(0o755)

    def run_install(self, *args, fault=None, profile_os=False):
        env = dict(self.env)
        if fault:
            env['DCORE_TEST_FAIL'] = fault
        if profile_os:
            env['DCORE_TEST_PROFILE_OS'] = '1'
        return subprocess.run([str(ROOT / 'scripts/incus_install'), NAME, *args],
                              env=env, text=True, capture_output=True, timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def state(self):
        return json.loads(self.state_path.read_text())

    def test_managed_os_and_temporary_root(self):
        result = self.run_install('--disk', '30GiB', '--pool', 'test-pool')
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(set(state['instances']), {NAME})
        self.assertEqual(state['volumes'], {})
        instance = state['instances'][NAME]
        self.assertTrue(instance['running'])
        self.assertEqual(instance['devices']['os']['path'], '/')
        self.assertEqual(instance['devices']['os']['size'], '30GiB')
        self.assertEqual(instance['devices']['os']['pool'], 'test-pool')
        self.assertNotIn('source', instance['devices']['os'])
        self.assertEqual(instance['devices']['root'], {'type': 'none'})
        calls = self.calls()
        status = calls.index(['incus', 'exec', NAME, '--', 'bootc', 'status'])
        deletion = calls.index(['incus', 'storage', 'volume', 'delete', 'test-pool', VOLUME])
        self.assertLess(status, deletion)
        edits = [c[-1] for c in calls if c[1:3] == ['config', 'edit']]
        self.assertEqual(edits[0]['devices']['root']['source'], VOLUME)
        self.assertEqual(edits[0]['devices']['root']['io.bus'], 'virtio-scsi')
        self.assertEqual(edits[0]['devices']['os']['io.bus'], 'virtio-blk')
        self.assertNotIn('path', edits[0]['devices']['root'])
        self.assertNotIn('OS volume:', result.stdout)

    def test_filesystems(self):
        for filesystem in (None, 'ext4', 'btrfs', 'xfs'):
            with self.subTest(filesystem=filesystem):
                self.state_path.write_text(json.dumps({'instances': {}, 'volumes': {}}))
                self.log.write_text('')
                result = self.run_install(*(['--filesystem', filesystem] if filesystem else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                install = next(c for c in self.calls() if 'to-disk' in c)
                self.assertEqual(install[install.index('--filesystem') + 1], filesystem or 'ext4')
                self.assertEqual('--allow-missing-verity' in install, filesystem == 'xfs')

    def test_ssh_key_reaches_installing_vm(self):
        key = self.work / 'key.pub'
        key.write_text('ssh-ed25519 test-fixture\n')
        result = self.run_install('--ssh-key', str(key))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(['incus', 'file', 'push', str(key), NAME + '/root/dcore-install.pub'], self.calls())
        install = next(c for c in self.calls() if 'to-disk' in c)
        self.assertEqual(install[install.index('--root-ssh-authorized-keys') + 1], '/run/dcore-install.pub')

    def test_failures_clean_created_resources(self):
        for fault in ('export', 'convert', 'edit', 'install', 'boot', 'status'):
            with self.subTest(fault=fault):
                result = self.run_install(fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state(), {'instances': {}, 'volumes': {}})

    def test_keep_on_failure_preserves_resources(self):
        for fault in ('install', 'boot', 'status'):
            with self.subTest(fault=fault):
                self.state_path.write_text(json.dumps({'instances': {}, 'volumes': {}}))
                self.log.write_text('')
                result = self.run_install('--keep-on-failure', fault=fault)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(set(self.state()['instances']), {NAME, HELPER})
                self.assertEqual(set(self.state()['volumes']), {VOLUME})
                self.assertFalse(any(c[1] == 'delete' or c[1:4] == ['storage', 'volume', 'delete']
                                     for c in self.calls()))

    def test_volume_cleanup_failure_is_reported(self):
        result = self.run_install('--keep-on-failure', fault='delete-volume')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Ready:', result.stdout)
        self.assertIn(VOLUME, self.state()['volumes'])
        self.assertEqual(self.state()['instances'][NAME]['devices']['root'], {'type': 'none'})

    def test_existing_profile_os_is_not_overwritten(self):
        result = self.run_install(profile_os=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Profile already defines an os device', result.stderr)
        self.assertFalse(any(c[1:3] == ['config', 'edit'] for c in self.calls()))
        self.assertEqual(self.state(), {'instances': {}, 'volumes': {}})

    def test_invalid_filesystems_and_release_conversion(self):
        for filesystem in ('zfs', 'btrfs', 'xfs'):
            args = ['--filesystem', filesystem]
            if filesystem != 'zfs':
                args += ['--release', 'v1.0.0']
            result = self.run_install(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.calls(), [])

    def test_release_keeps_its_managed_disk(self):
        result = self.run_install('--release', 'v1.0.0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.state()['instances']), {NAME})
        self.assertEqual(self.state()['volumes'], {})
        self.assertEqual(self.state()['instances'][NAME]['devices']['root']['path'], '/')
        self.assertFalse(any('to-disk' in c or c[1:3] == ['image', 'export'] for c in self.calls()))


if __name__ == '__main__':
    unittest.main(verbosity=2)

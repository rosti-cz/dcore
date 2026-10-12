#!/usr/bin/python3
"""Pinned host image sources, ordinary-user sessions and privileged phase safety."""
import importlib.util
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
sys.path.insert(0, str(ROOT/'scripts/iso'))
from session import HostInstallerSession
spec = importlib.util.spec_from_file_location('host_backend', ROOT/'scripts/iso/host-backend.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)
spec = importlib.util.spec_from_file_location('password_tools', ROOT/'scripts/lib/install-password.py')
passwords = importlib.util.module_from_spec(spec)
spec.loader.exec_module(passwords)
DIGEST = 'sha256:' + 'a'*64
CONFIG = {'mode':'host', 'source_image':'registry.example/dcore@'+DIGEST,
          'local_image':'registry.example/dcore@'+DIGEST, 'tracking_image':'registry.example/dcore:trixie'}
IDENTITY = {'device':'/dev/fixture', 'major_minor':'8:16', 'diskseq':41, 'size':10*1024**3}
SETTINGS = {'device':'/dev/fixture', 'identity':IDENTITY, 'filesystem':'btrfs', 'encrypt':True}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = host.Backend.__new__(host.Backend)
        self.backend.directory = Path(self.temp.name)
        self.backend.checker = types.SimpleNamespace(check=mock.Mock(return_value=IDENTITY))
        self.backend.passwords = passwords
        self.backend.checks = types.SimpleNamespace(check_encryption_support=mock.Mock())
        host.write_json(self.backend.directory, 'source.json', CONFIG)
        self.backend.settings(SETTINGS)

    def test_source_pins_booted_version_and_preserves_tracking_tag_and_registry_port(self):
        status={'status':{'booted':{'image':{'image':{'image':'registry.example:5000/team/dcore:trixie','transport':'registry'},'imageDigest':DIGEST}}}}
        config=host.source_from_status(status)
        self.assertEqual(config['source_image'],'registry.example:5000/team/dcore@'+DIGEST)
        self.assertEqual(config['tracking_image'],'registry.example:5000/team/dcore:trixie')

    def test_nonbootc_host_and_missing_digest_are_refused(self):
        for value in ({'status':{'booted':None}}, {'status':{'booted':{'image':{'image':{'image':'example/dcore:trixie'}}}}}):
            with self.assertRaises(ValueError): host.source_from_status(value)

    def test_cached_image_skips_download_and_checks_digest(self):
        identifier = 'c'*64
        with mock.patch.object(self.backend, 'cached_image', return_value=identifier), \
             mock.patch.object(host, 'run') as run, \
             mock.patch.object(host.shutil, 'disk_usage') as usage:
            self.backend.load()
        run.assert_not_called()
        usage.assert_not_called()
        self.assertEqual(self.backend.source()['local_image'], identifier)

    def test_tagged_cache_works_without_digest_alias_and_ignores_newer_tag(self):
        expected = 'c'*64
        outcomes = [types.SimpleNamespace(returncode=0, stdout=expected+'\n'),
                    types.SimpleNamespace(returncode=125, stdout=''),
                    types.SimpleNamespace(returncode=0, stdout=json.dumps([{'Digest':'sha256:'+'b'*64,'Id':'d'*64}])),
                    types.SimpleNamespace(returncode=0, stdout=json.dumps([{'Digest':DIGEST,'Id':expected}]))]
        with mock.patch.object(host.subprocess, 'run', side_effect=outcomes):
            self.assertEqual(self.backend.cached_image(CONFIG), expected)

    def test_missing_image_pulls_exact_digest(self):
        with mock.patch.object(self.backend, 'cached_image', side_effect=[None, 'c'*64]), \
             mock.patch.object(host, 'run') as run, \
             mock.patch.object(host.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=20*1024**3)):
            self.backend.load()
        run.assert_called_once_with('podman', 'pull', CONFIG['source_image'])

    def test_insufficient_host_storage_stops_before_download(self):
        with mock.patch.object(self.backend, 'cached_image', return_value=None), \
             mock.patch.object(host, 'run') as run, \
             mock.patch.object(host.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=1024**3)):
            with self.assertRaisesRegex(ValueError, '10 GiB free'): self.backend.load()
        run.assert_not_called()

    def test_cached_digest_mismatch_is_refused(self):
        with mock.patch.object(self.backend, 'cached_image', return_value=None), \
             mock.patch.object(host, 'run'), \
             mock.patch.object(host.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=20*1024**3)):
            with self.assertRaisesRegex(ValueError, 'does not match'): self.backend.load()

    def test_disk_replacement_is_refused_before_write(self):
        self.backend.checker.check.return_value={'diskseq':42}
        with mock.patch.object(host,'run') as run:
            with self.assertRaisesRegex(ValueError,'identity or size changed'):
                self.backend.phase('install',{'confirmation':'ERASE /dev/fixture'})
            run.assert_not_called()

    def test_erase_and_privileged_deployment_use_stdin_only(self):
        request={'confirmation':'ERASE /dev/fixture','core_password':'private-core-fixture','luks_passphrase':'private-disk-fixture'}
        with mock.patch.object(host,'run') as run, \
             mock.patch.object(host.subprocess,'check_output',return_value=str(4*1024**3)), \
             mock.patch.object(host.shutil,'disk_usage',return_value=types.SimpleNamespace(free=20*1024**3)):
            self.backend.phase('install',request)
        command=run.call_args.args
        self.assertIn('--defer-account',command)
        self.assertIn('--local',command)
        self.assertIn('--encrypt',command)
        self.assertIn(CONFIG['source_image'],command)
        self.assertEqual(passwords.read_credentials(__import__('io').BytesIO(run.call_args.kwargs['input'])),(b'private-core-fixture',b'private-disk-fixture'))
        self.assertNotIn('private-core-fixture',str(command))
        self.assertNotIn('private-disk-fixture',(self.backend.directory/'settings.json').read_text())

    def test_deployed_os_cannot_be_erased_again(self):
        host.write_json(self.backend.directory,'layout.json',{'deployed':True})
        with mock.patch.object(host,'run') as run:
            with self.assertRaisesRegex(ValueError,'already deployed'):
                self.backend.phase('install',{'confirmation':'ERASE /dev/fixture'})
            with self.assertRaisesRegex(ValueError,'already deployed'):
                self.backend.settings(SETTINGS)
            run.assert_not_called()

    def test_backend_does_not_accept_direct_ordinary_execution(self):
        with mock.patch.object(host.sys,'argv',['backend','source']), \
             mock.patch.object(host.os,'geteuid',return_value=1000), \
             mock.patch.dict(host.os.environ,{'SUDO_UID':'1000'}), \
             mock.patch.object(host,'Backend') as backend:
            with self.assertRaisesRegex(ValueError,'ordinary user through djust'):host.main()
            backend.assert_not_called()


class HostSessionTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid()==0:self.skipTest('frontend tests run as an ordinary user')
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.remote={'layout':None,'settings':None}
        self.operations=[]
        def query(operation,value=None):
            if operation=='source':return CONFIG
            if operation=='status':return self.remote.copy()
            if operation=='settings':self.remote['settings']=value
            self.operations.append(operation)
        self.query=mock.patch.object(HostInstallerSession,'query_backend',side_effect=query)
        self.query.start();self.addCleanup(self.query.stop)
        self.session=HostInstallerSession(directory=Path(self.temp.name)/'session',install_dir=ROOT/'scripts')
        self.addCleanup(self.session.close)
        self.session.settings('/dev/fixture',IDENTITY,'btrfs',True)
        self.session.credentials(b'private-core-fixture',b'private-disk-fixture')

    def test_ordinary_frontend_uses_sudo_only_for_backend_operations(self):
        with mock.patch.object(self.session,'command') as command:
            self.session.run_phase('install','ERASE /dev/fixture')
        args,payload,_=command.call_args.args
        self.assertEqual(args[:3],['/usr/bin/sudo','--non-interactive','--'])
        self.assertEqual(args[-1],'install')
        self.assertNotIn('private-core-fixture',str(args))
        self.assertEqual(json.loads(payload)['core_password'],'private-core-fixture')
        self.assertNotIn('private-core-fixture',(self.session.directory/'session.json').read_text())

    def test_host_mode_has_no_live_iso_ram_preflight(self):
        with mock.patch.object(self.session.checks,'check_resources') as check:
            self.session.check_resources()
        check.assert_not_called()

    def test_root_backend_checkpoint_recovers_failed_deployment_cleanup(self):
        def command(args,payload,log):
            if args[-1]=='install':
                self.remote['layout']={'deployed':True,'clean':False}
                raise ValueError('Injected close failure')
        with mock.patch.object(self.session,'command',side_effect=command):
            self.assertFalse(self.session.run('ERASE /dev/fixture'))
        self.assertEqual(self.session.state['failed'],'cleanup')
        self.assertIn('install',self.session.state['completed'])

    def test_restarting_interface_restores_settings_from_root_checkpoint(self):
        self.session.close()
        (self.session.directory/'session.json').unlink()
        self.remote['layout']={'deployed':True,'configured':True,'clean':True}
        resumed=HostInstallerSession(directory=self.session.directory,install_dir=ROOT/'scripts')
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.state['settings'],SETTINGS)
        self.assertIn('configure',resumed.state['completed'])
        self.assertIsNone(resumed.core_password)


if __name__=='__main__':unittest.main(verbosity=2)

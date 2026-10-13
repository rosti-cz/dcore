#!/usr/bin/python3
"""Pinned host image sources, ordinary-user sessions and privileged phase safety."""
import importlib.util
import array
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts/iso'))
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
                self.backend.phase('install',{'confirmed':True})
            run.assert_not_called()

    def test_install_requires_explicit_boolean_confirmation(self):
        for value in (None, False, 'true', 1, 'ERASE /dev/fixture'):
            with self.subTest(value=value), mock.patch.object(host, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'confirmation'):
                    self.backend.phase('install', {'confirmed': value})
                run.assert_not_called()

    def test_erase_and_privileged_deployment_use_stdin_only(self):
        request={'confirmed':True,'core_password':'private-core-fixture','luks_passphrase':'private-disk-fixture'}
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
                self.backend.phase('install',{'confirmed':True})
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

    def test_live_load_uses_bundled_archive_without_registry(self):
        self.backend.live = True
        self.backend.checks.check_resources = mock.Mock()
        with mock.patch.object(self.backend, 'cached_image', side_effect=[None, 'c'*64]), \
             mock.patch.object(host, 'run') as run:
            self.backend.load()
        run.assert_called_once_with('podman', 'load', '--input', str(host.MEDIUM/'dcore/image.oci.tar.gz'))
        self.assertEqual(self.backend.checks.check_resources.call_count, 1)
        self.assertTrue(self.backend.checks.check_resources.call_args.kwargs['require_free'])

    def test_live_media_is_excluded_again_before_install(self):
        self.backend.live = True
        self.backend.checks.media_devices = mock.Mock(return_value={'/dev/fixture'})
        with mock.patch.object(host, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'installer medium'):
                self.backend.phase('install', {'confirmed':True})
        run.assert_not_called()

    def test_enrollment_uses_memfd_hash_and_never_plaintext_arguments(self):
        host.write_json(self.backend.directory, 'layout.json',
            dict(deployed=True, configured=True, verified=True, clean=True))
        calls = []
        def execute(*args, **kwargs):
            calls.append((args, kwargs))
            descriptor = kwargs['pass_fds'][0]
            self.assertEqual(os.read(descriptor, 256), b'$6$test$hashed\n')
            self.assertIn('--hash-file', args)
            self.assertNotIn('temporary-pin', str(args))
        status = subprocess.CompletedProcess([], 0, stdout='certificate is not enrolled', stderr='')
        hashed = subprocess.CompletedProcess([], 0, stdout=b'$6$test$hashed\n', stderr=b'')
        with mock.patch.object(host.Path, 'is_dir', return_value=True), \
             mock.patch.object(host.Path, 'is_file', return_value=True), \
             mock.patch.object(host.subprocess, 'run', side_effect=[status, hashed]), \
             mock.patch.object(host, 'run', side_effect=execute):
            self.backend.enroll(dict(enrollment_password='temporary-pin', enrollment_confirm='temporary-pin'))
        self.assertEqual(len(calls), 1)
        self.assertNotIn('temporary-pin', (self.backend.directory/'layout.json').read_text())
        with self.assertRaises(OSError): os.fstat(calls[0][1]['pass_fds'][0])

    def test_enrollment_refuses_before_configuration_or_for_invalid_password(self):
        with mock.patch.object(host, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Complete'):
                self.backend.enroll({})
            run.assert_not_called()
        host.write_json(self.backend.directory, 'layout.json',
            dict(deployed=True, configured=True, verified=True, clean=True))
        with mock.patch.object(host.Path, 'is_dir', return_value=True), \
             mock.patch.object(host.Path, 'is_file', return_value=True), \
             mock.patch.object(host.subprocess, 'run') as run:
            for password in ('', 'x'*17, 'nonascii-ž', 'bad\nline'):
                with self.assertRaises(ValueError):
                    self.backend.enroll(dict(enrollment_password=password, enrollment_confirm=password))
            run.assert_not_called()

    def test_reboot_only_on_verified_clean_live_deployment(self):
        host.write_json(self.backend.directory, 'layout.json',
            dict(deployed=True, configured=True, verified=True, clean=True))
        with mock.patch.object(host, 'run') as run:
            self.backend.live = False
            with self.assertRaises(ValueError): self.backend.reboot()
            self.backend.live = True
            self.backend.reboot()
            run.assert_called_once_with('systemctl', 'reboot')

    def test_web_listener_rejects_public_directory_before_binding(self):
        info = types.SimpleNamespace(st_mode=0o40755, st_uid=1000)
        with mock.patch.object(host.Path, 'lstat', return_value=info), \
             mock.patch.object(host.socket, 'socket') as socket:
            with self.assertRaisesRegex(ValueError, 'private'): host.bind_web(1000)
            socket.assert_not_called()

    def test_web_listener_transfers_a_live_fd_to_verified_caller(self):
        directory = self.backend.directory/'dcore-installer'
        directory.mkdir(mode=0o700)
        original = socket.socket
        server = original(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(directory/'listener.sock')); server.listen(1)
        received = []
        failures = []
        def receive():
            try:
                connection, _ = server.accept()
                with connection:
                    data, ancillary, _, _ = connection.recvmsg(32, socket.CMSG_SPACE(array.array('i').itemsize))
                    self.assertEqual(data, b'dcore-http')
                    for level, kind, contents in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            descriptors = array.array('i'); descriptors.frombytes(contents)
                            received.extend(descriptors)
            except BaseException as error: failures.append(error)
        class EphemeralSocket(original):
            def bind(self, address):
                return super().bind(('::1', 0) if self.family == socket.AF_INET6 else ('127.0.0.1', 0))
        def create(family=-1, kind=-1, proto=-1, fileno=None):
            if fileno is not None:
                return original(family, kind, proto, fileno=fileno)
            return original(family, kind) if family == socket.AF_UNIX else EphemeralSocket(family, kind)
        thread = threading.Thread(target=receive, daemon=True); thread.start()
        with mock.patch.object(host, 'Path', return_value=directory), mock.patch.object(host.socket, 'socket', side_effect=create):
            host.bind_web(os.geteuid())
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(len(received), 1)
        with original(fileno=received[0]) as listener:
            self.assertTrue(listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
            self.assertGreater(listener.getsockname()[1], 0)


if __name__=='__main__':unittest.main(verbosity=2)

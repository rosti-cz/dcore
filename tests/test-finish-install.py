#!/usr/bin/python3
"""Configuration and cleanup retries must never format a deployed disk."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('finish_install', ROOT / 'scripts/lib/finish-install.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

class FinishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.state = self.work / 'state'
        self.state.mkdir()
        self.layout = {'version':1, 'deployed':True, 'root_partition':'/dev/test3', 'root_uuid':'root-fixture', 'mapper':None}
        (self.state / 'layout.json').write_text(json.dumps(self.layout))
        self.calls = []
        original = helper.tempfile.mkdtemp
        self.directory = mock.patch.object(helper.tempfile, 'mkdtemp', side_effect=lambda prefix, dir: original(prefix=prefix, dir=self.work))
        self.field = mock.patch.object(helper, 'field', return_value='root-fixture')
        self.write = mock.patch.object(helper.password_tools, 'write_layout', side_effect=lambda directory, data: (directory / 'layout.json').write_text(json.dumps(data)))
        self.run = mock.patch.object(helper.subprocess, 'run', side_effect=self.execute)
        for patch in (self.directory,self.write,self.run):patch.start()
        self.field_mock=self.field.start()

    def execute(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ('bootc','install','mount'):
            (Path(args[-1]) / 'etc').mkdir()
        return types.SimpleNamespace(returncode=0)

    def tearDown(self):
        for patch in (self.run,self.write,self.field,self.directory):patch.stop()
        self.temp.cleanup()

    def test_configuration_failure_can_retry_without_formatting(self):
        with mock.patch.object(helper.password_tools, 'provision_core_password', side_effect=[ValueError('fixture failure'), None]) as configure:
            with self.assertRaisesRegex(ValueError, 'fixture failure'):
                helper.finish('configure', self.state, io.BytesIO(b'private-fixture'))
            self.assertTrue(json.loads((self.state / 'layout.json').read_text())['clean'])
            helper.finish('configure', self.state, io.BytesIO(b'private-fixture'))
            self.assertEqual(configure.call_count, 2)
        self.assertFalse(any(args[0] in ('wipefs','sgdisk') or str(args[0]).startswith('mkfs') for args in self.calls))
        self.assertNotIn('private-fixture', str(self.calls) + (self.state / 'layout.json').read_text())
        self.assertEqual(sum(args[0] == 'umount' for args in self.calls),4)

    def test_changed_partition_refused_before_mount(self):
        self.field_mock.return_value = 'replacement'
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            helper.finish('configure',self.state,io.BytesIO(b'fixture'))
        self.assertEqual(self.calls,[])

    def test_cleanup_is_password_free_and_only_closes_owned_mapping(self):
        self.layout.update(mapper='dcore-finish-123456abcdef',luks_uuid='root-fixture')
        (self.state/'layout.json').write_text(json.dumps(self.layout))
        self.field_mock.side_effect = ['root-fixture','crypto_LUKS']
        with mock.patch.object(helper,'close_owned_mapping') as close:
            helper.finish('cleanup',self.state,io.BytesIO(b''))
            close.assert_called_once()
            self.assertEqual(close.call_args.args[0]['mapper'], self.layout['mapper'])
        self.assertEqual(self.calls,[])
        self.assertTrue(json.loads((self.state/'layout.json').read_text())['clean'])

    def test_foreign_mapping_refused(self):
        self.layout['mapper']='dcore-finish-123456abcdef'
        with mock.patch.object(helper.Path,'exists',return_value=True), \
             mock.patch.object(helper.subprocess,'check_output',return_value=' device: /dev/another3\n'):
            with self.assertRaisesRegex(ValueError, 'another device'):
                helper.close_owned_mapping(self.layout)
        self.assertEqual(self.calls,[])

    def test_unmount_failure_does_not_mark_cleanup_complete(self):
        def execute(args,**kwargs):
            result=self.execute(args,**kwargs)
            if args[0]=='umount':result.returncode=1
            return result
        with mock.patch.object(helper.subprocess,'run',side_effect=execute), \
             mock.patch.object(helper.password_tools,'provision_core_password'):
            with self.assertRaisesRegex(ValueError,'Cannot unmount'):
                helper.finish('configure',self.state,io.BytesIO(b'fixture'))
        self.assertFalse(json.loads((self.state/'layout.json').read_text())['clean'])

if __name__=='__main__':unittest.main(verbosity=2)

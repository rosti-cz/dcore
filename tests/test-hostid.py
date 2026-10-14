#!/usr/bin/python3
"""Installed ZFS identities must be unique and survive image upgrades."""
import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader(
    'provision_hostid', str(ROOT / 'rootfs/usr/libexec/dcore/provision-hostid'))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class HostidTests(unittest.TestCase):
    def test_first_boot_generates_nonzero_identity_and_keeps_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'etc').mkdir()
            with mock.patch.object(helper.os, 'urandom', side_effect=[bytes(4), b'\xd7\x57\x33\x46']):
                helper.provision(root)
            path = root / 'etc/hostid'
            self.assertEqual(path.read_bytes(), b'\xd7\x57\x33\x46')
            with mock.patch.object(helper.os, 'urandom', side_effect=AssertionError('identity rotated')):
                helper.provision(root)
            self.assertEqual(path.read_bytes(), b'\xd7\x57\x33\x46')

    def test_hosts_from_same_generic_image_get_distinct_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            hosts = [Path(directory) / name for name in ('one', 'two')]
            for host in hosts:
                (host / 'etc').mkdir(parents=True)
                helper.provision(host)
            self.assertNotEqual(*(host.joinpath('etc/hostid').read_bytes() for host in hosts))

    def test_invalid_existing_identity_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'etc').mkdir()
            path = root / 'etc/hostid'
            for invalid in (b'', bytes(4), b'invalid'):
                path.write_bytes(invalid)
                with self.assertRaisesRegex(ValueError, 'refusing to replace'):
                    helper.provision(root)
                self.assertEqual(path.read_bytes(), invalid)


if __name__ == '__main__':
    unittest.main(verbosity=2)

#!/usr/bin/python3
"""Ensure pre-unlock growth targets only the explicitly configured dcore root."""
import importlib.machinery
import types
from pathlib import Path
import sys
import unittest
sys.dont_write_bytecode = True
source = Path(__file__).resolve().parents[1] / 'rootfs/usr/libexec/dcore/grow-root'
helper = types.ModuleType('grow_root')
importlib.machinery.SourceFileLoader('grow_root', str(source)).exec_module(helper)
UUID = 'b01a1200-1234-4321-8888-123456789abc'
CMDLINE = f'root=/dev/mapper/dcore-root rd.luks.name={UUID}=dcore-root composefs'
class RootSelection(unittest.TestCase):
    def test_select_root_and_ignore_other_volumes(self):
        self.assertEqual(helper.initrd_partition('dcore-root', CMDLINE + ' rd.luks.name=other=data'), '/dev/disk/by-uuid/' + UUID)
    def test_refuse_other_cryptsetup_instances(self):
        with self.assertRaises(helper.Skip): helper.initrd_partition('data', CMDLINE)
    def test_refuse_missing_or_ambiguous_kernel_root(self):
        for line in (CMDLINE.replace('root=/dev/mapper/dcore-root ', ''), CMDLINE + ' root=/dev/mapper/data'):
            with self.assertRaises(helper.Skip): helper.initrd_partition('dcore-root', line)
    def test_refuse_missing_ambiguous_or_malformed_uuid(self):
        for line in ('root=/dev/mapper/dcore-root', CMDLINE + f' rd.luks.name={UUID}=dcore-root', CMDLINE.replace(UUID, '../../data')):
            with self.assertRaises(helper.Skip): helper.initrd_partition('dcore-root', line)
if __name__ == '__main__': unittest.main(verbosity=2)

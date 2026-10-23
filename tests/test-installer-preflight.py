#!/usr/bin/env python3
"""Source-image dependency failures must be reported before disk operations."""
import importlib.util
from pathlib import Path
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('preflight', ROOT/'scripts/lib/preflight-install.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightTests(unittest.TestCase):
    def test_missing_tools_stop_before_loading_crypto_or_encryption_helpers(self):
        with mock.patch.object(preflight.shutil, 'which', return_value=None), \
             mock.patch.object(preflight.ctypes, 'CDLL') as library, \
             mock.patch.object(preflight.importlib.util, 'spec_from_file_location') as helper:
            with self.assertRaisesRegex(ValueError, 'bootc.*mkfs.ext4'):
                preflight.check('ext4', True)
            library.assert_not_called()
            helper.assert_not_called()

    def test_selected_filesystem_formatter_is_required(self):
        for filesystem in ('ext4', 'btrfs', 'xfs'):
            formatter = 'mkfs.' + filesystem
            with self.subTest(filesystem=filesystem), \
                 mock.patch.object(preflight.shutil, 'which', side_effect=lambda tool: None if tool == formatter else '/fixture/'+tool):
                with self.assertRaisesRegex(ValueError, formatter):
                    preflight.check(filesystem, False)

    def test_plain_install_does_not_require_encryption_helpers(self):
        with mock.patch.object(preflight.shutil, 'which', return_value='/fixture/tool'), \
             mock.patch.object(preflight.ctypes, 'CDLL'), \
             mock.patch.object(preflight.importlib.util, 'spec_from_file_location') as helper:
            preflight.check('btrfs', False)
            helper.assert_not_called()

    def test_encryption_validates_tools_and_initramfs_before_install(self):
        module = types.SimpleNamespace(preflight=mock.Mock(side_effect=ValueError('missing cryptsetup')))
        with mock.patch.object(preflight.shutil, 'which', return_value='/fixture/tool'), \
             mock.patch.object(preflight.ctypes, 'CDLL'), \
             mock.patch.object(preflight.importlib.util, 'spec_from_file_location'), \
             mock.patch.object(preflight.importlib.util, 'module_from_spec', return_value=module):
            with self.assertRaisesRegex(ValueError, 'missing cryptsetup'):
                preflight.check('xfs', True)
            module.preflight.assert_called_once_with()


if __name__ == '__main__':
    unittest.main(verbosity=2)

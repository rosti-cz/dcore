#!/usr/bin/env python3
"""Exercise Debian ownership annotations in a disposable chroot (requires root)."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HELPER = Path(__file__).resolve().parents[1] / 'scripts/chunk-components.py'


@unittest.skipUnless(os.geteuid() == 0, 'chroot requires root')
class ComponentsTest(unittest.TestCase):
    def test_merged_usr_and_absolute_links_stay_inside_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('usr/bin', 'usr/lib/sysimage/dpkg/info', 'usr/share/dcore'):
                (root / name).mkdir(parents=True)
            (root / 'bin').symlink_to('/usr/bin')
            executable = root / 'usr/bin/tool'
            executable.write_bytes(b'package payload')
            executable.chmod(0o755)
            (root / 'usr/bin/tool-link').symlink_to('/usr/bin/tool')
            (root / 'usr/share/dcore/config').write_text('local payload')
            (root / 'usr/lib/sysimage/dpkg/info/example.list').write_text(
                '/.\n/usr\n/usr/bin\n/bin/tool\n/bin/tool-link\n/missing\n')
            subprocess.run(['python3', str(HELPER), str(root)], check=True)
            self.assertEqual(os.getxattr(executable, 'user.component'), b'deb/example')
            self.assertEqual(executable.read_bytes(), b'package payload')
            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
            self.assertNotIn('user.component', os.listxattr(root / 'usr/bin'))
            self.assertEqual(os.getxattr(root / 'usr/share/dcore', 'user.component'), b'dcore')
            self.assertEqual(os.readlink(root / 'bin'), '/usr/bin')

    def test_refuses_missing_package_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['python3', str(HELPER), directory], capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

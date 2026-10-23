#!/usr/bin/python3
"""Validate the source image's installation tools without changing a disk."""
import argparse
import ctypes
import ctypes.util
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True


def check(filesystem, encrypt):
    tools = ('bootc', 'lsblk', 'blkid', 'udevadm', 'mount', 'umount', 'sync',
             'chpasswd', 'sfdisk', 'wipefs', 'blockdev', 'mkfs.vfat',
             {'ext4': 'mkfs.ext4', 'btrfs': 'mkfs.btrfs', 'xfs': 'mkfs.xfs'}[filesystem])
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        raise ValueError('Source image lacks installation tools: ' + ', '.join(missing))
    ctypes.CDLL(ctypes.util.find_library('crypt') or 'libcrypt.so.1')
    if encrypt:
        spec = importlib.util.spec_from_file_location(
            'encrypted_install', Path(__file__).with_name('install-encrypted.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.preflight()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--filesystem', choices=('ext4', 'btrfs', 'xfs'), required=True)
    parser.add_argument('--encrypt', action='store_true')
    args = parser.parse_args()
    try:
        check(args.filesystem, args.encrypt)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f'Installation dependency check failed; disk unchanged: {error}')

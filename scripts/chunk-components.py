#!/usr/bin/env python3
"""Annotate a disposable Debian rootfs for chunkah; never run on a live host."""
import os
from pathlib import Path
import stat
import sys


def annotate():
    info = '/usr/lib/sysimage/dpkg/info'
    lists = sorted(name for name in os.listdir(info) if name.endswith('.list'))
    if not lists:
        raise SystemExit('No relocated dpkg file lists found')
    tagged = 0
    for listing in lists:
        component = ('deb/' + listing.removesuffix('.list')).encode()
        with open(info + '/' + listing, encoding='utf-8') as stream:
            names = stream.read().splitlines()
        for name in names:
            path = name
            # Shared directories must not assign their descendants to a package.
            # Symlink xattrs are unsupported on Linux; these stay in the catch-all.
            if not os.path.isabs(path) or not os.path.isfile(path) or os.path.islink(path):
                continue
            os.setxattr(path, 'user.component', component)
            os.setxattr(path, 'user.update-interval', b'monthly')
            tagged += 1
    for name, component in (
        ('/usr/libexec/dcore', 'dcore'),
        ('/usr/share/dcore', 'dcore'),
        ('/usr/lib/bootc', 'bootc'),
        ('/usr/bin/bootc', 'bootc'),
    ):
        path = name
        if os.path.exists(path) and not os.path.islink(path):
            os.setxattr(path, 'user.component', component.encode())
            os.setxattr(path, 'user.update-interval', b'weekly')
    # Hardlinks can share attributes with a file in a different group. Set the
    # interval consistently on every explicitly claimed regular file afterwards.
    for directory, _, files in os.walk('/'):
        for name in files:
            path = os.path.join(directory, name)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            attrs = os.listxattr(path)
            if 'user.component' in attrs:
                component = os.getxattr(path, 'user.component')
                os.setxattr(path, 'user.update-interval',
                            b'monthly' if component.startswith(b'deb/') else b'weekly')
    print(f'Assigned {tagged} package file entries from {len(lists)} dpkg lists')


if __name__ == '__main__':
    if len(sys.argv) != 2 or Path(sys.argv[1]).resolve() == Path('/'):
        raise SystemExit('Usage: chunk-components.py DISPOSABLE_ROOTFS (not /)')
    # Resolve absolute and merged-/usr symlinks inside the image, never against
    # the build host. The helper exits immediately after annotating the rootfs.
    os.chroot(sys.argv[1])
    os.chdir('/')
    annotate()

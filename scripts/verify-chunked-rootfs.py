#!/usr/bin/env python3
"""Compare mounted image trees, allowing chunking attributes and new mtimes."""
import hashlib
import os
from pathlib import Path
import stat
import sys


def inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            metadata = path.lstat()
            attrs = {
                key: os.getxattr(path, key, follow_symlinks=False)
                for key in os.listxattr(path, follow_symlinks=False)
                if key not in ('user.component', 'user.update-interval', 'security.selinux')
            }
            payload = None
            if stat.S_ISREG(metadata.st_mode):
                with path.open('rb') as stream:
                    payload = hashlib.file_digest(stream, 'sha256').hexdigest()
            elif stat.S_ISLNK(metadata.st_mode):
                payload = os.readlink(path)
            result[str(path.relative_to(root))] = (
                metadata.st_mode, metadata.st_uid, metadata.st_gid, attrs, payload)
    return result


if __name__ == '__main__':
    source, chunked = (inventory(Path(name)) for name in sys.argv[1:])
    differences = sorted(name for name in source.keys() | chunked.keys()
                         if source.get(name) != chunked.get(name))
    if differences:
        raise SystemExit('Rechunking changed filesystem entries: ' + ', '.join(differences[:30]))
    print(f'Filesystem verified: {len(source)} entries retain content, ownership, modes and xattrs')

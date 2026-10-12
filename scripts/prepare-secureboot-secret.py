#!/usr/bin/python3
"""Write the CI signing secret to a private file without printing its value."""
import os
from pathlib import Path
import subprocess

key = os.environ.get('SECUREBOOT_SIGNING_KEY', '')
if not key:
    raise SystemExit('SECUREBOOT_SIGNING_KEY is required for published images.')
path = Path(os.environ['RUNNER_TEMP']) / 'dcore-secureboot.key'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd, 'w') as output:
        output.write(key)
    public = subprocess.run(['openssl', 'pkey', '-in', str(path), '-pubout'],
                            check=True, capture_output=True).stdout
    certificate = subprocess.run(['openssl', 'x509', '-in', 'secureboot/dcore.crt',
                                  '-pubkey', '-noout'], check=True, capture_output=True).stdout
    if public != certificate:
        raise ValueError('GitHub signing secret does not match the public certificate.')
except BaseException:
    path.unlink(missing_ok=True)
    raise

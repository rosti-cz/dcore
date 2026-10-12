#!/usr/bin/python3
"""Repeat account/verification/cleanup stages on an already deployed disk."""
import argparse
import ctypes
import ctypes.util
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
import uuid

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('password_tools', Path(__file__).with_name('install-password.py'))
password_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(password_tools)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def field(device, name):
    return subprocess.check_output(['blkid', '-s', name, '-o', 'value', device], text=True).strip()


def close_owned_mapping(layout):
    mapper = layout.get('mapper')
    if not mapper or not Path('/dev/mapper', mapper).exists():
        return
    if not re.fullmatch(r'dcore-(?:install|finish)-[a-f0-9]{12}', mapper):
        raise ValueError('Unexpected installer mapping name; refusing cleanup.')
    status = subprocess.check_output(['cryptsetup', 'status', mapper], text=True)
    match = re.search(r'^\s*device:\s*(\S+)', status, re.M)
    if not match or os.path.realpath(match[1]) != os.path.realpath(layout['root_partition']):
        raise ValueError('Installer mapping belongs to another device; refusing cleanup.')
    run('cryptsetup', 'close', mapper)


def finish(operation, state_dir, stream):
    layout = json.loads((state_dir / 'layout.json').read_text())
    if layout.get('version') != 1 or layout.get('deployed') is not True:
        raise ValueError('No completed OS deployment is recorded.')
    partition = layout['root_partition']
    if layout.get('luks_uuid'):
        if field(partition, 'UUID') != layout['luks_uuid'] or field(partition, 'TYPE') != 'crypto_LUKS':
            raise ValueError('Encrypted partition identity changed.')
    elif field(partition, 'UUID') != layout['root_uuid']:
        raise ValueError('Root filesystem identity changed.')
    if operation == 'cleanup':
        close_owned_mapping(layout)
        layout['clean'] = True
        password_tools.write_layout(state_dir, layout)
        return

    if layout.get('luks_uuid'):
        core_password, passphrase = password_tools.read_credentials(stream)
    else:
        core_password = password_tools.validate_password(stream.read(4097))
        passphrase = None
    close_owned_mapping(layout)
    mountpoint = Path(tempfile.mkdtemp(prefix='dcore-finish-', dir='/run'))
    deployment = mountpoint / 'deployment'
    sysroot = mountpoint / 'sysroot'
    deployment.mkdir()
    sysroot.mkdir()
    descriptor = None
    root_mounted = deployed = opened = False
    device = partition
    layout['clean'] = False
    try:
        if passphrase is not None:
            descriptor = os.memfd_create('dcore-finish-unlock', os.MFD_CLOEXEC)
            os.write(descriptor, passphrase)
            os.lseek(descriptor, 0, os.SEEK_SET)
            mapper = 'dcore-finish-' + uuid.uuid4().hex[:12]
            layout['mapper'] = mapper
            password_tools.write_layout(state_dir, layout)
            run('cryptsetup', 'open', '--key-file', f'/proc/self/fd/{descriptor}', partition,
                mapper, pass_fds=(descriptor,))
            opened = True
            os.close(descriptor)
            descriptor = None
            device = '/dev/mapper/' + mapper
        if field(device, 'UUID') != layout['root_uuid']:
            raise ValueError('Root filesystem identity changed.')
        run('mount', device, str(sysroot))
        root_mounted = True
        run('bootc', 'install', 'mount', '--sysroot', str(sysroot), '--latest', str(deployment))
        deployed = True
        if operation == 'configure':
            password_tools.provision_core_password(deployment, core_password)
            layout['configured'] = True
        else:
            if not layout.get('configured'):
                raise ValueError('Administrator configuration is not complete.')
            shadow = deployment / 'etc/shadow'
            core = next((line.split(':')[1] for line in shadow.read_text().splitlines()
                         if line.startswith('core:')), '')
            if not core or core.startswith(('!', '*')):
                raise ValueError('The core password is still locked.')
            library = ctypes.CDLL(ctypes.util.find_library('crypt') or 'libcrypt.so.1')
            library.crypt.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
            library.crypt.restype = ctypes.c_char_p
            actual = library.crypt(core_password, core.encode('utf-8'))
            if actual is None or not hmac.compare_digest(actual, core.encode('utf-8')):
                raise ValueError('The supplied core password does not match the installed account.')
            config = deployment / 'etc/ssh/sshd_config.d/20-dcore-core-password.conf'
            if not config.is_file() or 'PasswordAuthentication yes' not in config.read_text():
                raise ValueError('Core SSH password access is not configured.')
            layout['verified'] = True
        run('sync', '-f', str(sysroot))
        password_tools.write_layout(state_dir, layout)
    finally:
        errors = []
        for mounted, path, recursive in ((deployed, deployment, True), (root_mounted, sysroot, False)):
            if mounted:
                result = subprocess.run(['umount'] + (['--recursive'] if recursive else []) + [str(path)])
                if result.returncode:
                    errors.append(f'Cannot unmount {path}')
        if opened:
            try:
                close_owned_mapping(layout)
            except (ValueError, OSError, subprocess.CalledProcessError):
                errors.append('Cannot close the installer encryption mapping')
        if descriptor is not None:
            os.close(descriptor)
        if not errors:
            layout['clean'] = True
            password_tools.write_layout(state_dir, layout)
        for path in (deployment, sysroot, mountpoint):
            try:
                path.rmdir()
            except OSError:
                pass
        if errors:
            raise ValueError('; '.join(errors))


if __name__ == '__main__':
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('configure', 'verify', 'cleanup'))
    parser.add_argument('--state-dir', required=True, type=Path)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError('This installation backend needs root.')
        finish(args.operation, args.state_dir, sys.stdin.buffer)
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        print(f'Installation {args.operation} failed: {error}', file=sys.stderr)
        sys.exit(1)

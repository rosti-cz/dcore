#!/usr/bin/python3
"""Install a disk and provision core's console/SSH password through stdin."""
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile

MAX_CREDENTIAL_BYTES = 65536


def write_layout(state_dir, data):
    if not state_dir:
        return
    directory = Path(state_dir)
    info = directory.stat()
    if directory.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('Installation state directory must be private and owned by root.')
    descriptor, temporary = tempfile.mkstemp(prefix='.layout-', dir=directory)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(data, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / 'layout.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def encode_credentials(core_password, luks_passphrase):
    """Versioned stdin protocol used only with --encrypt --core-password-stdin."""
    return json.dumps({'version': 1,
                       'core_password': validate_password(core_password).decode('utf-8'),
                       'luks_passphrase': validate_password(luks_passphrase).decode('utf-8')},
                      ensure_ascii=False).encode('utf-8')


def read_credentials(stream):
    # Reject malformed/oversized input without echoing any part of a secret.
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate field')
            result[key] = value
        return result

    try:
        raw = stream.read(MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise ValueError('Oversized input')
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
        if not isinstance(data, dict) or set(data) != {'version', 'core_password', 'luks_passphrase'}:
            raise ValueError('Invalid fields')
        if type(data['version']) is not int or data['version'] != 1:
            raise ValueError('Invalid version')
        if not all(isinstance(data[name], str) for name in ('core_password', 'luks_passphrase')):
            raise ValueError('Invalid secret type')
        return (validate_password(data['core_password'].encode('utf-8')),
                validate_password(data['luks_passphrase'].encode('utf-8')))
    except (ValueError, TypeError, RecursionError):
        raise ValueError('Invalid encrypted-install credential payload; disk unchanged.') from None


def enable_core_password_ssh(deployment):
    directory = deployment / 'etc/ssh/sshd_config.d'
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / '20-dcore-core-password.conf'
    # Match overrides the image's global PasswordAuthentication no for core.
    # Reset the scope so subsequent includes retain their normal interpretation.
    config.write_text('Match User core\n    PasswordAuthentication yes\nMatch all\n')
    config.chmod(0o644)


def validate_password(password):
    if not password or len(password) > 4096 or any(c in password for c in (b'\n', b'\r', b'\0')):
        raise ValueError('Password must contain 1 to 4096 bytes, without newline or NUL.')
    try:
        password.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('Password must be UTF-8 text.') from None
    return password


def provision_core_password(deployment, password):
    validate_password(password)
    # Use libcrypt directly, avoiding PAM runtime mounts in the offline chroot.
    subprocess.run(['chpasswd', '--root', str(deployment), '--crypt-method', 'YESCRYPT'],
                   input=b'core:' + password + b'\n', check=True)
    enable_core_password_ssh(deployment)


def root_partition(device):
    data = json.loads(subprocess.check_output(
        ['lsblk', '--json', '--paths', '--output', 'NAME,KNAME,TYPE', device], text=True))
    for child in data['blockdevices'][0].get('children', []):
        if child['type'] == 'part':
            number = Path('/sys/class/block') / Path(child['kname']).name / 'partition'
            if number.read_text().strip() == '3':
                return child['name']
    raise ValueError('Installed root partition was not found.')


def install(device, options, password, defer_account=False, state_dir=None):
    # Validate before bootc can erase anything. No password in argv or files.
    validate_password(password)
    subprocess.run(['bootc', 'install', 'to-disk', device, '--wipe', *options], check=True)
    subprocess.run(['udevadm', 'settle'], check=True)
    if defer_account:
        root = root_partition(device)
        uuid = subprocess.check_output(['blkid', '-s', 'UUID', '-o', 'value', root], text=True).strip()
        write_layout(state_dir, {'version': 1, 'device': device, 'root_partition': root,
                                'root_uuid': uuid, 'luks_uuid': None, 'mapper': None,
                                'deployed': True, 'clean': True})
        return
    work = tempfile.mkdtemp(prefix='dcore-password-', dir='/run')
    try:
        sysroot = Path(work) / 'sysroot'
        deployment = Path(work) / 'deployment'
        sysroot.mkdir()
        deployment.mkdir()
        mounted = deployed = False
        try:
            subprocess.run(['mount', root_partition(device), str(sysroot)], check=True)
            mounted = True
            subprocess.run(['bootc', 'install', 'mount', '--sysroot', str(sysroot),
                            '--latest', str(deployment)], check=True)
            deployed = True
            provision_core_password(deployment, password)
            subprocess.run(['sync', '-f', str(sysroot)], check=True)
        finally:
            # Refuse to remove mountpoints if cleanup fails.
            try:
                if deployed:
                    subprocess.run(['umount', '--recursive', str(deployment)], check=True)
            finally:
                if mounted:
                    subprocess.run(['umount', str(sysroot)], check=True)
    finally:
        # rmdir cannot traverse or delete files on a still-mounted filesystem.
        for path in (Path(work) / 'deployment', Path(work) / 'sysroot', Path(work)):
            try:
                path.rmdir()
            except OSError:
                pass


if __name__ == '__main__':
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        options = sys.argv[2:]
        defer = '--defer-account' in options
        state_dir = None
        if defer:
            options.remove('--defer-account')
            position = options.index('--state-dir')
            state_dir = options[position + 1]
            del options[position:position + 2]
        install(sys.argv[1], options, sys.stdin.buffer.read(4097), defer, state_dir)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # CalledProcessError for chpasswd contains argv, never its stdin.
        print(f'Installation/password provisioning failed: {error}', file=sys.stderr)
        sys.exit(1)

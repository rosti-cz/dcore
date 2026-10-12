#!/usr/bin/python3
"""Install a disk and provision core's console/SSH password through stdin."""
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile


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


def root_partition(device):
    data = json.loads(subprocess.check_output(
        ['lsblk', '--json', '--paths', '--output', 'NAME,KNAME,TYPE', device], text=True))
    for child in data['blockdevices'][0].get('children', []):
        if child['type'] == 'part':
            number = Path('/sys/class/block') / Path(child['kname']).name / 'partition'
            if number.read_text().strip() == '3':
                return child['name']
    raise ValueError('Installed root partition was not found.')


def install(device, options, password):
    # Validate before bootc can erase anything. No password in argv or files.
    validate_password(password)
    subprocess.run(['bootc', 'install', 'to-disk', device, '--wipe', *options], check=True)
    subprocess.run(['udevadm', 'settle'], check=True)
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
            # Explicit libcrypt method avoids needing PAM's runtime mounts in
            # an offline chroot. The account already exists in the dcore image.
            subprocess.run(['chpasswd', '--root', str(deployment), '--crypt-method', 'YESCRYPT'],
                           input=b'core:' + password + b'\n', check=True)
            enable_core_password_ssh(deployment)
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
        install(sys.argv[1], sys.argv[2:], sys.stdin.buffer.read(4097))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # CalledProcessError for chpasswd contains argv, never its stdin.
        print(f'Installation/password provisioning failed: {error}', file=sys.stderr)
        sys.exit(1)

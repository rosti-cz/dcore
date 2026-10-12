#!/usr/bin/python3
"""Passphrase-only LUKS2 provisioning; secrets travel through stdin and memfd."""
import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True


def validate_passphrase(value):
    if not value or len(value) > 4096:
        raise ValueError('Passphrase must contain 1 to 4096 UTF-8 bytes.')
    if any(character in value for character in (b'\n', b'\r', b'\0')):
        raise ValueError('Passphrase must be a single line without NUL characters.')
    try:
        value.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('Passphrase must be UTF-8 text for console unlocking.') from error
    return value


def read_passphrase_file(path):
    # Resolve no symlinks, and verify the actual open descriptor before reading.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        owners = {0, int(os.environ.get('SUDO_UID', os.getuid()))}
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or stat.S_IMODE(info.st_mode) not in (0o400, 0o600):
            raise ValueError('Passphrase file must be a regular file owned by you/root with mode 0600 or 0400.')
        value = os.read(descriptor, 4099)
        if value.endswith(b'\n'):
            value = value[:-1]
            if value.endswith(b'\r'):
                value = value[:-1]
        return validate_passphrase(value)
    finally:
        os.close(descriptor)


def prompt_passphrase():
    if not sys.stdin.isatty():
        raise ValueError('--encrypt needs a terminal passphrase prompt or --passphrase-file.')
    first = getpass.getpass('Disk encryption passphrase: ')
    second = getpass.getpass('Confirm disk encryption passphrase: ')
    if first != second:
        raise ValueError('Passphrases do not match; disk unchanged.')
    return validate_passphrase(first.encode('utf-8'))


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def host_install(args):
    secret = read_passphrase_file(args.passphrase_file) if args.passphrase_file else prompt_passphrase()
    # Prompting can take minutes. Repeat the same host-side identity/busy check
    # immediately before crossing into the container's private mount namespace.
    checker_path = Path(__file__).with_name('check-block-device.py')
    spec = importlib.util.spec_from_file_location('block_check', checker_path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    if os.path.realpath(args.requested_device) != args.device:
        raise ValueError('Destination path changed; disk unchanged.')
    if checker.check(args.device) != json.loads(args.identity):
        raise ValueError('Destination identity or size changed; disk unchanged.')
    command = [
        'podman', 'run', '--rm', '--pull=never', '-i', '--privileged',
        '--pid=host', '--ipc=host', '--security-opt', 'label=disable',
        '-v', '/dev:/dev', '-v', '/var/lib/containers:/var/lib/containers',
        '-v', f'{Path(__file__).resolve()}:/run/dcore-install-encrypted.py:ro',
        '-v', f'{args.ssh_key}:/run/dcore-install.pub:ro', args.image,
        'python3', '/run/dcore-install-encrypted.py', '--inside',
        '--device', args.device, '--filesystem', args.filesystem,
        '--image', args.image, '--ssh-key', '/run/dcore-install.pub',
    ]
    if getattr(args, 'target_image', None):
        command += ['--target-image', args.target_image]
    if getattr(args, 'local', False):
        command.append('--local')
        command.insert(2, '--network=none')
    if args.no_grow_root:
        command.append('--no-grow-root')
    # Neither the file path nor its contents are passed into the container.
    run(command, input=secret)


def preflight():
    for name in ('cryptsetup', 'sfdisk', 'wipefs', 'blockdev', 'udevadm',
                 'mount', 'umount', 'mkfs.vfat', 'blkid', 'bootc', 'lsinitrd', 'sync', 'lsblk'):
        if not shutil.which(name):
            raise ValueError(f'Image lacks required encryption tool: {name}; disk unchanged.')
    if not Path('/usr/lib/systemd/systemd-cryptsetup').is_file():
        raise ValueError('Image lacks systemd-cryptsetup; disk unchanged.')
    kernels = list(Path('/usr/lib/modules').glob('*/initramfs.img'))
    if len(kernels) != 1:
        raise ValueError('Image must contain exactly one initramfs; disk unchanged.')
    inventory = subprocess.check_output(['lsinitrd', str(kernels[0])], text=True)
    if 'dm-crypt.ko' not in inventory:
        raise ValueError('Image initramfs lacks dm-crypt; disk unchanged.')
    modules = subprocess.check_output(['lsinitrd', '-m', str(kernels[0])], text=True).splitlines()
    for module in ('crypt', 'systemd-cryptsetup', 'bootc'):
        if module not in modules:
            raise ValueError(f'Image initramfs lacks {module}; disk unchanged. Build a current dcore image.')


def partitions(device):
    # Identify partitions by kernel PARTN, never guess sdX/NVMe/loop naming.
    for _ in range(50):
        data = json.loads(subprocess.check_output(
            ['lsblk', '--json', '--paths', '--output', 'NAME,KNAME,TYPE', device], text=True))
        found = {}
        for child in data['blockdevices'][0].get('children', []):
            if child.get('type') != 'part':
                continue
            number = Path('/sys/class/block') / Path(child['kname']).name / 'partition'
            if number.is_file():
                found[int(number.read_text())] = child['name']
        if 2 in found and 3 in found and all(Path(found[number]).is_block_device() for number in (2, 3)):
            return found[2], found[3]
        time.sleep(0.1)
    raise ValueError('New disk partitions did not appear.')


def inside_install(args):
    if os.geteuid() != 0 or not Path(args.device).is_block_device():
        raise ValueError('Encrypted installation requires root and a block device.')
    preflight()
    formatter = {'ext4': 'mkfs.ext4', 'btrfs': 'mkfs.btrfs', 'xfs': 'mkfs.xfs'}[args.filesystem]
    if not shutil.which(formatter):
        raise ValueError(f'Image lacks {formatter}; disk unchanged.')
    if int(subprocess.check_output(['blockdev', '--getsize64', args.device])) < 10 * 1024**3:
        raise ValueError('Encrypted installation requires a disk of at least 10 GiB; disk unchanged.')
    secret = validate_passphrase(sys.stdin.buffer.read(4097))
    descriptor = os.memfd_create('dcore-luks-passphrase', os.MFD_CLOEXEC)
    os.write(descriptor, secret)
    del secret
    key_path = f'/proc/self/fd/{descriptor}'
    mapper = f'dcore-install-{uuid.uuid4().hex[:12]}'
    mapping_open = False
    root_mounted = False
    esp_mounted = False
    deployment_mounted = False
    deployment = Path(tempfile.mkdtemp(prefix='dcore-encrypted-deployment-', dir='/run'))
    mountpoint = Path(tempfile.mkdtemp(prefix='dcore-encrypted-', dir='/run'))
    try:
        run(['wipefs', '--all', args.device])
        # Keep bootc's BIOS/ESP/root numbering; all mutable OS data is in LUKS.
        layout = (
            'label: gpt\n'
            'size=1M, type=21686148-6449-6e6f-744e-656564454649\n'
            'size=2G, type=c12a7328-f81f-11d2-ba4b-00a0c93ec93b\n'
            'type=ca7d7ccb-63ed-4c53-861c-1742536059cc\n'
        )
        run(['sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', args.device],
            input=layout, text=True)
        # udev may briefly hold the new partitions while probing wiped labels.
        # Do not format anything until the kernel accepts the new partition map.
        for attempt in range(10):
            run(['udevadm', 'settle'])
            result = subprocess.run(['blockdev', '--rereadpt', args.device],
                                    check=False, capture_output=True, text=True)
            if result.returncode == 0:
                break
            if attempt == 9:
                raise ValueError('Cannot reread the new partition table; device is busy.')
            time.sleep(0.5)
        run(['udevadm', 'settle'])
        esp, encrypted = partitions(args.device)
        run(['mkfs.vfat', '-F', '32', '-n', 'DCORE_EFI', esp])
        os.lseek(descriptor, 0, os.SEEK_SET)
        run(['cryptsetup', 'luksFormat', '--batch-mode', '--type', 'luks2',
             '--pbkdf', 'argon2id', '--pbkdf-memory', '262144', '--iter-time', '2000',
             '--key-file', key_path, encrypted], pass_fds=(descriptor,))
        os.lseek(descriptor, 0, os.SEEK_SET)
        run(['cryptsetup', 'open', '--key-file', key_path, encrypted, mapper], pass_fds=(descriptor,))
        mapping_open = True
        os.close(descriptor)
        descriptor = None
        mapped_device = f'/dev/mapper/{mapper}'
        if args.filesystem == 'ext4':
            run([formatter, '-F', '-O', 'verity', mapped_device])
        elif args.filesystem == 'btrfs':
            run([formatter, '-f', mapped_device])
        else:
            run([formatter, '-f', mapped_device])
        run(['mount', mapped_device, str(mountpoint)])
        root_mounted = True
        (mountpoint / 'boot').mkdir()
        run(['mount', '-o', 'umask=0077', esp, str(mountpoint / 'boot')])
        esp_mounted = True
        boot_uuid = subprocess.check_output(['blkid', '-s', 'UUID', '-o', 'value', esp], text=True).strip()
        luks_uuid = subprocess.check_output(['cryptsetup', 'luksUUID', encrypted], text=True).strip()
        command = [
            'bootc', 'install', 'to-filesystem', str(mountpoint),
            '--composefs-backend', '--bootloader', 'systemd', '--disable-selinux',
            '--generic-image', '--root-mount-spec', '/dev/mapper/dcore-root',
            # bootc otherwise emits systemd.mount-extra=...:ro for /boot.
            '--boot-mount-spec', '',
            '--target-imgref', getattr(args, 'target_image', None) or args.image, '--root-ssh-authorized-keys', args.ssh_key,
            '--karg', f'rd.luks.name={luks_uuid}=dcore-root', '--karg', 'console=tty0',
        ]
        if getattr(args, 'local', False):
            command.append('--skip-fetch-check')
        if args.no_grow_root:
            command += ['--karg', 'dcore.grow-root=0']
        if args.filesystem == 'xfs':
            command.append('--allow-missing-verity')
        run(command)
        # With a mapped root, GPT auto-discovery cannot remount the ESP like it
        # does on a plain disk. Provision writable, private ESP access explicitly.
        # Use bootc's supported mount API to reach /etc across native layouts.
        run(['mount', '-o', 'remount,rw', str(mountpoint)])
        deployment_mounted = True
        run(['bootc', 'install', 'mount', '--sysroot', str(mountpoint), '--latest', str(deployment)])
        fstab = deployment / 'etc/fstab'
        existing = fstab.read_text() if fstab.exists() else ''
        if any(line.split()[1:2] == ['/boot'] for line in existing.splitlines()
               if line.strip() and not line.lstrip().startswith('#')):
            raise ValueError('Source image already configures /boot; refusing to create conflicting boot mount.')
        with fstab.open('a') as output:
            if existing and not existing.endswith('\n'):
                output.write('\n')
            output.write(f'UUID={boot_uuid} /boot vfat rw,nosuid,nodev,umask=0077 0 2\n')
        run(['umount', '--recursive', str(deployment)])
        deployment_mounted = False
        # Standalone install-finalize is OSTree-only in bootc 1.17; the normal
        # to-filesystem call already finalized this native installation. Flush
        # our single configuration edit and restore its read-only mount.
        run(['sync', '-f', str(mountpoint)])
        run(['mount', '-o', 'remount,ro', str(mountpoint)])
        print('Installed LUKS2 root. Unlock it with your passphrase at each boot; no TPM or stored unlock key.')
    finally:
        # Always release the secret descriptor, even when a mount cannot detach.
        # Attempt every cleanup operation and preserve directories still mounted.
        original_error = sys.exc_info()[0]
        cleanup_errors = []
        cleanup_mounts_ok = True
        try:
            for mounted, path, recursive in (
                    (deployment_mounted, deployment, True),
                    (esp_mounted, mountpoint / 'boot', False),
                    (root_mounted, mountpoint, False)):
                if mounted:
                    try:
                        command = ['umount'] + (['--recursive'] if recursive else []) + [str(path)]
                        result = subprocess.run(command, check=False)
                        if result.returncode:
                            raise OSError('unmount returned a failure status')
                    except OSError:
                        cleanup_mounts_ok = False
                        cleanup_errors.append(f'cannot unmount {path}')
            if mapping_open:
                try:
                    result = subprocess.run(['cryptsetup', 'close', mapper], check=False)
                    if result.returncode:
                        raise OSError('mapping close returned a failure status')
                except OSError:
                    cleanup_errors.append(f'cannot close {mapper}')
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if cleanup_mounts_ok:
            shutil.rmtree(mountpoint)
            shutil.rmtree(deployment)
        if cleanup_errors:
            print('Cleanup incomplete: ' + '; '.join(cleanup_errors), file=sys.stderr)
            if original_error is None:
                raise ValueError('Encrypted installation cleanup failed; inspect the reported mappings.')



def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--device', required=True)
    parser.add_argument('--filesystem', choices=('ext4', 'btrfs', 'xfs'), required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--target-image')
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--ssh-key', required=True)
    parser.add_argument('--passphrase-file')
    parser.add_argument('--no-grow-root', action='store_true')
    parser.add_argument('--requested-device')
    parser.add_argument('--identity')
    args = parser.parse_args()
    if args.inside:
        inside_install(args)
    else:
        if not args.requested_device or not args.identity:
            raise ValueError('Host installation requires the checked destination identity.')
        host_install(args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt, EOFError) as error:
        print(f'Encrypted installation failed: {error}', file=sys.stderr)
        sys.exit(1)

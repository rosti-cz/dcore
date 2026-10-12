#!/usr/bin/python3
"""Offline console installer. Runs as root on the live ISO, outside containers."""
import getpass
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
INSTALL = Path('/usr/libexec/dcore/install')
MEDIUM = Path('/run/live/medium')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def media_devices():
    source = subprocess.check_output(
        ['findmnt', '--noheadings', '--output', 'SOURCE', '--mountpoint', str(MEDIUM)], text=True).strip()
    if not source or not Path(source).is_block_device():
        raise ValueError('Cannot identify the installer medium; refusing disk selection.')
    return set(subprocess.check_output(
        ['lsblk', '--inverse', '--noheadings', '--paths', '--output', 'NAME', source], text=True).split())


def eligible_disks(checker, excluded):
    disks = json.loads(subprocess.check_output(
        ['lsblk', '--json', '--paths', '--bytes', '--output', 'NAME,TYPE,SIZE,MODEL,SERIAL,TRAN'],
        text=True))['blockdevices']
    result = []
    for disk in disks:
        if disk['type'] != 'disk' or disk['name'] in excluded or int(disk['size']) < 10 * 1024**3:
            continue
        try:
            identity = checker.check(disk['name'])
        except (ValueError, OSError, subprocess.CalledProcessError):
            continue
        result.append((disk, identity))
    return result


def choose(title, options, default=0):
    print(f'\n{title}')
    for index, option in enumerate(options, 1):
        print(f'{index}. {option}')
    answer = input(f'Select a number [{default + 1}], or q to cancel: ').strip()
    if answer.lower() == 'q':
        return None
    if not answer:
        return default
    if not answer.isdecimal() or not 1 <= int(answer) <= len(options):
        raise ValueError('Invalid selection; disk unchanged.')
    return int(answer) - 1


def check_encryption_support():
    for tool in ('modprobe', 'dmsetup'):
        if not shutil.which(tool):
            raise ValueError(f'Missing live encryption tool: {tool}; disk unchanged.')
    if not Path('/usr/lib/udev/rules.d/95-dm-notify.rules').is_file():
        raise ValueError('The live environment lacks device-mapper udev notification rules; disk unchanged.')
    # Container cryptsetup uses the live kernel; load its driver on the host
    # before any erase, using the live environment's matching module tree.
    result = subprocess.run(['modprobe', '--quiet', 'dm-crypt'], capture_output=True)
    if result.returncode:
        raise ValueError('The live kernel cannot load dm-crypt; disk unchanged.')


def install():
    if not Path('/sys/firmware/efi').is_dir():
        raise ValueError('Boot the installer in UEFI mode with Secure Boot disabled.')
    config = json.loads((MEDIUM / 'dcore/installer.json').read_text())
    checker = module('block_check', INSTALL / 'lib/check-block-device.py')
    password_tools = module('password_tools', INSTALL / 'lib/install-password.py')
    choices = eligible_disks(checker, media_devices())
    print(f"\nOffline dcore installer — {config['release']} ({config['debian_release']})")
    print('UEFI installation. The selected drive will be completely erased.')
    print('The core password enables console and SSH login.')
    if not choices:
        raise ValueError('No unused whole drives of at least 10 GiB are available.')
    for index, (disk, _) in enumerate(choices, 1):
        print(f"{index}. {disk['name']}  {int(disk['size']) / 1024**3:.1f} GiB  "
              f"{disk.get('model') or ''}  serial={disk.get('serial') or 'unknown'}")
    selection = input('Select a drive number, or Enter to cancel: ').strip()
    if not selection:
        return
    if not selection.isdecimal() or not 1 <= int(selection) <= len(choices):
        raise ValueError('Invalid drive number.')
    disk, identity = choices[int(selection) - 1]
    selected_fs = choose('Root filesystem:', ['ext4 (default)', 'btrfs', 'xfs'])
    if selected_fs is None:
        return
    filesystem = ('ext4', 'btrfs', 'xfs')[selected_fs]
    selected_encryption = choose('Root encryption:', ['No encryption', 'LUKS2 (passphrase required at boot)'])
    if selected_encryption is None:
        return
    encrypt = selected_encryption == 1
    if encrypt:
        check_encryption_support()
        print('Unlock the disk at the console on every boot, including update reboots.')
        print('SSH is available after unlocking. TPM unlocking can be enrolled after installation.')
    first = getpass.getpass('Password for core: ')
    second = getpass.getpass('Confirm password: ')
    if first != second:
        raise ValueError('Passwords do not match; disk unchanged.')
    password = password_tools.validate_password(first.encode('utf-8'))
    del first, second
    if encrypt:
        first = getpass.getpass('Disk encryption passphrase (separate from core login): ')
        second = getpass.getpass('Confirm disk encryption passphrase: ')
        if first != second:
            raise ValueError('Disk passphrases do not match; disk unchanged.')
        passphrase = password_tools.validate_password(first.encode('utf-8'))
        payload = password_tools.encode_credentials(password, passphrase)
        del first, second, passphrase
    else:
        payload = password
    del password
    device = disk['name']
    print(f'\nDestination: {device}; filesystem: {filesystem}; encryption: {"LUKS2" if encrypt else "none"}.')
    if input(f'Type ERASE {device} to continue: ') != f'ERASE {device}':
        print('Cancelled; disk unchanged.')
        return
    print('Loading the bundled dcore image into RAM. This can take several minutes…')
    subprocess.run(['podman', 'load', '--input', str(MEDIUM / 'dcore/image.oci.tar.gz')], check=True)
    if device in media_devices() or checker.check(device) != identity:
        raise ValueError('Destination changed during preparation; disk unchanged.')
    command = [str(INSTALL / 'block_install'), device, '--local', '--filesystem', filesystem,
                    '--image', config['local_image'], '--target-image', config['tracking_image'],
                    '--core-password-stdin', '--confirm-device', device]
    if encrypt:
        command.append('--encrypt')
    subprocess.run(command, input=payload, check=True)
    del payload
    print('\nInstallation complete. Remove the installer media before booting the new system.')
    if input('Type REBOOT to reboot now, or Enter to stay here: ') == 'REBOOT':
        subprocess.run(['systemctl', 'reboot'], check=True)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if os.geteuid() != 0 or not sys.stdin.isatty():
        raise ValueError('The installer needs a root console terminal.')
    while True:
        try:
            install()
        except (ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt, EOFError) as error:
            print(f'\nInstaller stopped: {error}. A failed write may leave the target incomplete.')
        action = input('\nEnter to restart the installer, or type SHELL for a rescue shell: ')
        if action == 'SHELL':
            subprocess.run(['/bin/bash'], check=False)


if __name__ == '__main__':
    main()

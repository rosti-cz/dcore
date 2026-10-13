"""Live installer resources, medium exclusion and guarded disk discovery."""
import json
import os
from pathlib import Path
import shutil
import subprocess

MEDIUM = Path('/run/live/medium')

GIB = 1024**3


def check_resources(config, require_free=False, staging=Path('/var/lib/containers'),
                    meminfo=Path('/proc/meminfo')):
    memory = {}
    for line in meminfo.read_text().splitlines():
        fields = line.split()
        if fields[0] in ('MemTotal:', 'MemAvailable:'):
            memory[fields[0][:-1]] = int(fields[1]) * 1024
    minimum = config.get('minimum_ram_bytes', 16 * GIB)
    # Firmware and kernel reservations reduce Linux's reported usable RAM.
    if memory['MemTotal'] < minimum - GIB:
        raise ValueError(f'This offline installer needs at least {minimum / GIB:.0f} GiB RAM; '
                         f'detected {memory["MemTotal"] / GIB:.1f} GiB. Increase the VM memory '
                         'and boot the ISO again. The destination disk has not been changed.')
    filesystem = subprocess.check_output(
        ['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', str(staging)], text=True).strip()
    if filesystem != 'tmpfs':
        raise ValueError('Image staging must be on the dedicated RAM filesystem; disk unchanged.')
    temporary = staging / 'image-tmp'
    temporary.mkdir(mode=0o700, exist_ok=True)
    info = temporary.stat()
    if temporary.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Image temporary directory must be private and owned by the installer.')
    # Podman image loading defaults to /var/tmp on the smaller live overlay.
    os.environ['TMPDIR'] = str(temporary)
    if require_free:
        needed = 2 * config.get('image_size_bytes', 4 * GIB) + GIB
        capacity = os.statvfs(staging)
        available = capacity.f_bavail * capacity.f_frsize
        if min(memory['MemAvailable'], available) < needed:
            raise ValueError(f'Image loading needs {needed / GIB:.1f} GiB free RAM staging space; '
                             f'available RAM {memory["MemAvailable"] / GIB:.1f} GiB, '
                             f'staging {available / GIB:.1f} GiB. Increase RAM or free staging space '
                             'before retrying. The destination disk has not been changed.')


def media_devices():
    source = subprocess.check_output(
        ['findmnt', '--noheadings', '--output', 'SOURCE', '--mountpoint', str(MEDIUM)], text=True).strip()
    if not source or not Path(source).is_block_device():
        raise ValueError('Cannot identify the installer medium; refusing disk selection.')
    return set(subprocess.check_output(
        ['lsblk', '--inverse', '--noheadings', '--paths', '--output', 'NAME', source], text=True).split())


def eligible_disks(checker, excluded):
    disks = json.loads(subprocess.check_output(
        ['lsblk', '--json', '--paths', '--bytes', '--output', 'NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,LABEL,MOUNTPOINTS'],
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

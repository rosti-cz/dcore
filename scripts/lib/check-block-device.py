#!/usr/bin/python3
"""Validate an unused whole block device and print its hotplug identity."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


def check(device):
    if os.readlink("/proc/self/ns/mnt") != os.readlink("/proc/1/ns/mnt"):
        raise ValueError("run in the host mount namespace so mounted disks are visible")
    metadata = os.stat(device)
    if not stat.S_ISBLK(metadata.st_mode):
        raise ValueError("target must be a whole block device")
    major, minor = os.major(metadata.st_rdev), os.minor(metadata.st_rdev)
    sysdevice = Path(f"/sys/dev/block/{major}:{minor}").resolve(strict=True)
    if (sysdevice / "partition").exists():
        raise ValueError("target is a partition; select its whole disk instead")
    data = subprocess.run(
        ["lsblk", "--json", "--paths", "--bytes", "--output",
         "NAME,KNAME,TYPE,SIZE,RO,MOUNTPOINTS,MAJ:MIN", device],
        check=True, text=True, capture_output=True)
    devices = json.loads(data.stdout)["blockdevices"]
    if len(devices) != 1 or devices[0]["type"] not in ("disk", "loop"):
        raise ValueError("target must be a disk, not a mapped or RAID device")
    identifiers = set()

    def walk(node):
        identifiers.add(node["maj:min"])
        if node.get("ro") in (True, 1, "1", "true"):
            raise ValueError(f"read-only device: {node['name']}")
        if any(node.get("mountpoints") or []):
            raise ValueError(f"mounted filesystem or active swap: {node['name']}")
        if node["type"] not in ("disk", "loop", "part"):
            raise ValueError(f"active storage mapping: {node['name']}")
        block = Path("/sys/class/block") / Path(node["kname"]).name
        if any((block / "holders").iterdir()):
            raise ValueError(f"active device holder (LVM, encryption or RAID): {node['name']}")
        for child in node.get("children", []):
            walk(child)

    walk(devices[0])
    # Check swaps separately as well as lsblk's [SWAP] mount marker.
    for line in Path("/proc/swaps").read_text().splitlines()[1:]:
        escaped = line.split()[0]
        name = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), escaped)
        value = os.stat(name)
        if stat.S_ISBLK(value.st_mode) and f"{os.major(value.st_rdev)}:{os.minor(value.st_rdev)}" in identifiers:
            raise ValueError(f"active swap device: {name}")
    # O_EXCL detects block-device claims in other mount namespaces too,
    # including a disk mounted only inside a container or used for swap.
    try:
        descriptor = os.open(device, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
    except OSError as error:
        raise ValueError("device is busy (mounted filesystem, swap, or an active mapping)") from error
    else:
        os.close(descriptor)
    # Linux's disk sequence changes when a device is replaced, including a USB
    # disk reappearing under the same /dev/sdX name during the registry pull.
    sequence = int((sysdevice / "diskseq").read_text())
    return {"device": os.path.realpath(device), "major_minor": f"{major}:{minor}",
            "diskseq": sequence, "size": int(devices[0]["size"])}


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("usage: check-block-device.py DEVICE")
        print(json.dumps(check(sys.argv[1]), sort_keys=True))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Cannot install: {error}", file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env bash
# Run ONLY inside the disposable dcore VM: restarts its ZFS boot services.
set -euo pipefail
[[ ${DCORE_DISPOSABLE_VM_TEST:-} == 1 && $EUID == 0 ]]
systemd-detect-virt --vm >/dev/null
[[ -z $(zpool list -H -o name) ]]
work=$(mktemp -d /var/tmp/dcore-zfs-boot.XXXXXX)
pool=dcore-boot-test
loops=()
stop_storage() {
    systemctl stop zfs-zed.service zfs.target zfs-mount.service zfs-load-keys.service zfs-import.target zfs-import-scan.service
}
cleanup() {
    zpool destroy "$pool" 2>/dev/null || true
    for disk in "${loops[@]}"; do losetup -d "$disk"; done
    rm -rf "$work"
    rm -f /etc/zfs/zpool.cache
}
trap cleanup EXIT
stop_storage
for member in a b; do
    truncate -s 256M "$work/$member.img"
    loops+=("$(losetup --find --show "$work/$member.img")")
done
head -c 32 /dev/urandom > "$work/key"
chmod 600 "$work/key"
zpool create -m "$work/mount" -O encryption=on -O keyformat=raw \
    -O "keylocation=file://$work/key" "$pool" mirror "${loops[@]}"
printf 'survives boot import\n' > "$work/mount/check"
zpool set cachefile=/etc/zfs/zpool.cache "$pool"
cp /etc/zfs/zpool.cache "$work/stale.cache"
zpool export "$pool"

# Mimic the obsolete MD v1.2 signatures found on core0, in ZFS's label padding.
python3 - "${loops[@]}" <<'PY'
import struct, sys
header = bytearray(256)
struct.pack_into('<II', header, 0, 0xa92b4efc, 1)
header[16:32] = bytes(range(16))
struct.pack_into('32s', header, 32, b'dcore-test:oldmd')
struct.pack_into('<Q', header, 144, 8)  # superblock sector offset
checksum = sum(struct.unpack('<64I', header))
struct.pack_into('<I', header, 216, (checksum & 0xffffffff) + (checksum >> 32))
for disk in sys.argv[1:]:
    with open(disk, 'r+b', buffering=0) as output:
        output.seek(0x1000)
        output.write(header)
PY
for disk in "${loops[@]}"; do
    wipefs --no-act --output TYPE "$disk" | grep -q linux_raid_member
done

# Reattach in reverse order so the old loop device names point at other members.
for disk in "${loops[@]}"; do losetup -d "$disk"; done
loops=()
for member in b a; do loops+=("$(losetup --find --show "$work/$member.img")"); done

for cache in absent stale; do
    if [[ $cache == stale ]]; then
        cp "$work/stale.cache" /etc/zfs/zpool.cache
    else
        rm -f /etc/zfs/zpool.cache
    fi
    # Starting only the mount service must pull in import and key loading.
    systemctl start zfs-mount.service
    [[ $(zfs get -H -o value keystatus "$pool") == available ]]
    [[ $(zfs get -H -o value mounted "$pool") == yes ]]
    grep -qx 'survives boot import' "$work/mount/check"
    zpool status -x "$pool" | grep -q 'is healthy'
    stop_storage
    zpool export "$pool"
done

# Missing or incorrect keys must leave the dataset locked and a visible error.
mv "$work/key" "$work/key.saved"
systemctl start zfs-mount.service || true
systemctl is-failed zfs-load-keys.service
[[ $(zfs get -H -o value keystatus "$pool") == unavailable ]]
[[ $(zfs get -H -o value mounted "$pool") == no ]]
head -c 32 /dev/urandom > "$work/key"
systemctl reset-failed zfs-load-keys.service zfs-mount.service
systemctl stop zfs-mount.service
systemctl start zfs-mount.service || true
systemctl is-failed zfs-load-keys.service
[[ $(zfs get -H -o value keystatus "$pool") == unavailable ]]
[[ $(zfs get -H -o value mounted "$pool") == no ]]
mv "$work/key.saved" "$work/key"
systemctl reset-failed zfs-load-keys.service zfs-mount.service
systemctl stop zfs-mount.service
systemctl start zfs-mount.service
grep -qx 'survives boot import' "$work/mount/check"
echo 'ZFS boot services: missing/stale cache, swapped devices, old MD signatures, keys and mounts passed.'

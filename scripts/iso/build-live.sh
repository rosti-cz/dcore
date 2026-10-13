#!/usr/bin/env bash
# Executed only inside the disposable privileged ISO builder.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 live-build xorriso squashfs-tools \
    grub-efi-amd64-bin grub-efi-amd64-signed shim-signed grub-pc-bin dosfstools mtools isolinux syslinux-common
# live-build includes MokManager only in its shim-only branch. Add it to the
# signed-GRUB branch before FAT sizing, for both optical and hybrid USB boots.
python3 - <<'PYMOK'
from pathlib import Path
path = Path('/usr/lib/live/build/binary_grub-efi')
script = path.read_text()
marker = '${_CHROOT_DIR}/grub-efi-temp/EFI/boot/boot\\$efi_name.efi\n'
addition = ('\t\tcp -a ${_CHROOT_DIR}/usr/lib/shim/mm\\$efi_name.efi.signed \\\n'
            '\t\t\t${_CHROOT_DIR}/grub-efi-temp/EFI/boot/mm\\$efi_name.efi\n')
if marker not in script:
    raise SystemExit('Unsupported live-build EFI layout; refusing an ISO without MokManager.')
path.write_text(script.replace(marker, marker + addition, 1))
PYMOK
cd /work
lb config --mode debian --distribution "$DEBIAN_RELEASE" --architecture amd64 \
    --binary-image iso-hybrid --bootloaders grub-efi --debian-installer none \
    --chroot-squashfs-compression-type xz \
    --archive-areas 'main contrib non-free-firmware' --apt-recommends false \
    --firmware-chroot false --uefi-secure-boot enable \
    --bootappend-live 'boot=live components ip=none' --iso-volume DCORE_INSTALL
mkdir -p config/package-lists config/includes.binary/dcore \
    config/includes.chroot/usr/libexec/dcore/install \
    config/includes.chroot/etc/containers \
    config/includes.chroot/etc/systemd/system/multi-user.target.wants
cat > config/package-lists/installer.list.chroot <<'PACKAGES'
linux-image-amd64
firmware-linux-free
firmware-misc-nonfree
firmware-bnx2
firmware-bnx2x
firmware-qlogic
firmware-realtek
live-boot
live-config
live-config-systemd
systemd-sysv
podman
python3
systemd-resolved
openssh-client
util-linux
kmod
dmsetup
udev
iproute2
ca-certificates
mokutil
openssl
PACKAGES
mkdir -p config/includes.chroot/usr/share/dcore/secureboot
install -m 0644 /work/payload/dcore.der config/includes.chroot/usr/share/dcore/secureboot/dcore.der
install -m 0755 /source/iso/enroll-secureboot config/includes.chroot/usr/libexec/dcore/enroll-secureboot
# Use a dedicated RAM filesystem for Podman, avoiding nested overlay mounts
# on the live root. No destination disk is used for image staging.
cat > config/includes.chroot/etc/containers/storage.conf <<'STORAGE'
[storage]
driver = "overlay"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
STORAGE
cat > config/includes.chroot/etc/containers/containers.conf <<'ENGINE'
[engine]
cgroup_manager = "cgroupfs"
image_copy_tmp_dir = "/var/lib/containers/image-tmp"
ENGINE
cp /work/payload/* config/includes.binary/dcore/
cp /source/block_install config/includes.chroot/usr/libexec/dcore/install/
cp -r /source/lib config/includes.chroot/usr/libexec/dcore/install/
mkdir -p config/includes.chroot/usr/libexec/dcore/installer
install -m 0755 /source/iso/dcore-installer /source/iso/host-backend.py /source/iso/checks.py \
    config/includes.chroot/usr/libexec/dcore/installer/
mkdir -p config/includes.chroot/etc/systemd/network
install -m 0755 /source/iso/network-bootstrap config/includes.chroot/usr/libexec/dcore/
install -m 0644 /source/iso/dcore-network-bootstrap.service config/includes.chroot/etc/systemd/system/
ln -s ../dcore-network-bootstrap.service config/includes.chroot/etc/systemd/system/multi-user.target.wants/dcore-network-bootstrap.service
for unit in systemd-networkd.service systemd-resolved.service; do
    ln -s /usr/lib/systemd/system/$unit config/includes.chroot/etc/systemd/system/multi-user.target.wants/$unit
done
ln -sfn /run/systemd/resolve/stub-resolv.conf config/includes.chroot/etc/resolv.conf
cat > config/includes.chroot/usr/libexec/dcore/iso-installer <<'LAUNCHER'
#!/bin/sh
set -eu
install -d -m 0700 /var/lib/containers/image-tmp
export TMPDIR=/var/lib/containers/image-tmp
exec /usr/libexec/dcore/installer/dcore-installer "$@"
LAUNCHER
chmod 0755 config/includes.chroot/usr/libexec/dcore/iso-installer
cat > config/includes.chroot/etc/systemd/system/var-lib-containers.mount <<'UNIT'
[Unit]
Description=RAM storage for the offline installation image

[Mount]
What=tmpfs
Where=/var/lib/containers
Type=tmpfs
Options=mode=0700,size=75%
UNIT
cat > config/includes.chroot/etc/systemd/system/dcore-installer.service <<'UNIT'
[Unit]
Description=Offline dcore installer
Requires=var-lib-containers.mount
After=systemd-user-sessions.service live-config.service var-lib-containers.mount
Conflicts=getty@tty1.service
ConditionPathExists=/run/live/medium/dcore/installer.json

[Service]
# Keep kernel informational messages from overwriting the terminal interface.
Type=simple
ExecStartPre=-/usr/bin/dmesg --console-level 3
ExecStart=/usr/libexec/dcore/iso-installer
StandardInput=tty-force
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
Restart=on-failure
RestartSec=3
LimitCORE=0

[Install]
WantedBy=multi-user.target
UNIT
ln -s ../dcore-installer.service config/includes.chroot/etc/systemd/system/multi-user.target.wants/dcore-installer.service
ln -s /dev/null config/includes.chroot/etc/systemd/system/getty@tty1.service
lb build
# Verify the bytes used by the optical El Torito image and hybrid USB ESP.
mcopy -i binary/boot/grub/efi.img ::EFI/boot/mmx64.efi /work/mokmanager-check.efi
cmp /usr/lib/shim/mmx64.efi.signed /work/mokmanager-check.efi
rm /work/mokmanager-check.efi

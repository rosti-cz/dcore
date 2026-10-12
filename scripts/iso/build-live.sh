#!/usr/bin/env bash
# Executed only inside the disposable privileged ISO builder.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends live-build xorriso squashfs-tools \
    grub-efi-amd64-bin grub-pc-bin dosfstools mtools isolinux syslinux-common
cd /work
lb config --mode debian --distribution "$DEBIAN_RELEASE" --architecture amd64 \
    --binary-image iso-hybrid --bootloaders grub-efi --debian-installer none \
    --chroot-squashfs-compression-type xz \
    --archive-areas 'main contrib non-free-firmware' --apt-recommends false \
    --firmware-chroot false --uefi-secure-boot disable \
    --bootappend-live 'boot=live components' --iso-volume DCORE_INSTALL
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
python3-textual
openssh-client
util-linux
kmod
dmsetup
udev
iproute2
ca-certificates
PACKAGES
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
install -m 0755 /source/iso/installer.py /source/iso/session.py /source/iso/interface.py \
    config/includes.chroot/usr/libexec/dcore/installer/
cat > config/includes.chroot/usr/libexec/dcore/iso-installer <<'LAUNCHER'
#!/bin/sh
set -eu
install -d -m 0700 /var/lib/containers/image-tmp
export TMPDIR=/var/lib/containers/image-tmp
exec /usr/bin/python3 /usr/libexec/dcore/installer/installer.py "$@"
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

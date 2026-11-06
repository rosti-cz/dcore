#!/usr/bin/env bash
# Run in a disposable official Debian container, with the repository at /source.
set -euo pipefail
[[ -e /run/.containerenv || -e /.dockerenv ]] || {
    echo 'Run this test inside a disposable Debian container.' >&2; exit 1;
}
: "${DEBIAN_RELEASE:?Set DEBIAN_RELEASE}"
: "${INCUS_CHANNEL:?Set INCUS_CHANNEL}"
export DEBIAN_FRONTEND=noninteractive
# Match the production base image, including stable's contrib packages.
sed -i 's/^Components: main$/Components: main contrib non-free-firmware/' /etc/apt/sources.list.d/debian.sources
# Exercise the production repository setup and complete package list, stopping
# before kernel/DKMS setup. Only the large package installation is simulated;
# the prerequisite CA certificates, curl and GnuPG are installed normally.
sed '/^mapfile -t kernels /,$d' /source/scripts/install-system.sh |
    sed '/^apt-get install -y --no-install-recommends \\$/s/ install / --simulate install /' > /tmp/package-selection.sh
bash /tmp/package-selection.sh
# Also install the real userspace stack in this initially ZFS-free container.
apt-get install -y --no-install-recommends systemd udev "zfsutils-linux/${DEBIAN_RELEASE}-backports" "zfs-zed/${DEBIAN_RELEASE}-backports"
version=$(dpkg-query -W -f='${Version}' zfsutils-linux)
[[ $version == *~bpo* ]]
for package in $(dpkg-query -W -f='${binary:Package} ${source:Package}\n' | awk '$2 == "zfs-linux" { print $1 }'); do
    [[ $(dpkg-query -W -f='${Version}' "$package") == "$version" ]]
done
# Check the dcore dependency graph against the actual packaged ZFS units.
for dropin in /source/rootfs/usr/lib/systemd/system/zfs*.d; do
    cp -r "$dropin" /usr/lib/systemd/system/
done
cp /source/rootfs/usr/lib/systemd/system/dcore-hostid.service /usr/lib/systemd/system/
install -D /source/rootfs/usr/libexec/dcore/provision-hostid /usr/libexec/dcore/provision-hostid
cp /source/rootfs/etc/systemd/system/zfs-load-keys.service /etc/systemd/system/
systemctl disable zfs-import-cache.service
systemctl enable zfs.target zfs-import.target zfs-import-scan.service zfs-load-keys.service zfs-mount.service zfs-zed.service dcore-hostid.service
systemd-analyze verify --man=no zfs.target zfs-import.target zfs-import-scan.service zfs-load-keys.service zfs-mount.service
# Ordinary packages must still prefer stable.
candidate=$(apt-cache policy podman | awk '/Candidate:/ { print $2 }')
[[ -n $candidate && $candidate != *~bpo* ]]
echo "Fresh package selection passed; OpenZFS userspace and libraries match $version."

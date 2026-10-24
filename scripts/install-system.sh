#!/usr/bin/env bash
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg
install -d -m 0755 /etc/apt/keyrings
curl --proto '=https' --tlsv1.2 -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc
fingerprint=$(gpg --batch --show-keys --with-colons /etc/apt/keyrings/zabbly.asc | awk -F: '$1 == "fpr" {print $10; exit}')
[[ "$fingerprint" == 4EFC590696CB15B87C73A3AD82CC8797C838DCFD ]]
case "$INCUS_CHANNEL" in stable|lts-6.0|lts-7.0|daily) ;; *) echo "Unsupported Incus channel" >&2; exit 1 ;; esac
cat > /etc/apt/sources.list.d/zabbly-incus.sources <<EOF
Types: deb
URIs: https://pkgs.zabbly.com/incus/${INCUS_CHANNEL}
Suites: ${DEBIAN_RELEASE}
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/zabbly.asc
EOF
# Keep the guest-agent integration from Debian; the server supplies its agent binary.
cat > /etc/apt/preferences.d/debian-incus-agent <<'EOF'
Package: incus-agent
Pin: release o=Debian
Pin-Priority: 1002
EOF
# Prefer Zabbly even if Debian ships a newer Incus package version.
cat > /etc/apt/preferences.d/zabbly-incus <<'EOF'
Package: incus incus-* libincus*
Pin: origin pkgs.zabbly.com
Pin-Priority: 1001
EOF
case "$(dpkg --print-architecture)" in
    amd64) kernel_arch=amd64 ;;
    arm64) kernel_arch=arm64 ;;
    *) echo "Only amd64 and arm64 are supported" >&2; exit 1 ;;
esac
# Opt in only the kernel and matching OpenZFS stack; other packages stay stable.
cat > /etc/apt/sources.list.d/debian-backports.sources <<EOF
Types: deb
URIs: https://deb.debian.org/debian
Suites: ${DEBIAN_RELEASE}-backports
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
# Keep every OpenZFS binary/library on the same backports version. Selecting
# just zfsutils-linux/suite leaves stable candidates for shared library names.
cat > /etc/apt/preferences.d/dcore-zfs-backports <<EOF
Package: src:zfs-linux
Pin: release n=${DEBIAN_RELEASE}-backports
Pin-Priority: 990
EOF
apt-get update
apt-get install -y --no-install-recommends \
    just tpm2-tools cryptsetup systemd-cryptsetup build-essential procps file distrobox uidmap slirp4netns fuse-overlayfs passt libpam-systemd dbus-user-session openvswitch-switch openvswitch-common btrfs-progs cloud-guest-utils gdisk python3 bubblewrap dosfstools e2fsprogs fdisk parted firmware-linux-free \
    "linux-image-${kernel_arch}/${DEBIAN_RELEASE}-backports" "linux-headers-${kernel_arch}/${DEBIAN_RELEASE}-backports" \
    "systemd-boot-efi-${kernel_arch}-signed" shim-signed mokutil sbsigntool openssl \
    iproute2 libnss-resolve libnss-myhostname openssh-server skopeo wireguard \
    systemd systemd-sysv systemd-boot systemd-boot-efi systemd-resolved \
    systemd-timesyncd smartmontools ipmitool nvme-cli sysstat tcpdump lm-sensors xfsprogs libostree-1-1 ostree dracut kmod podman sudo \
    "zfs-dkms/${DEBIAN_RELEASE}-backports" "zfsutils-linux/${DEBIAN_RELEASE}-backports" \
    "zfs-zed/${DEBIAN_RELEASE}-backports" "zfs-dracut/${DEBIAN_RELEASE}-backports" incus incus-agent \
    fish fastfetch fzf locales locales-all tzdata htop vim nmap git tmux wget curl jq unzip iotop ncdu rsync httpie bat \
    ripgrep iputils-tracepath duf docker.io docker-cli docker-compose rclone \
    bzip2 zstd
mapfile -t kernels < <(find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
[[ ${#kernels[@]} == 1 ]]
kernel=${kernels[0]}
# Explicit target: uname -r is the builder host kernel, not our image kernel.
dkms autoinstall -k "$kernel"
modinfo -k "$kernel" zfs > /dev/null
depmod -a "$kernel"
install -m 0644 "/boot/vmlinuz-${kernel}" "/usr/lib/modules/${kernel}/vmlinuz"
# Record provenance before the immutable rootfs conversion clears /var.
install -d /usr/share/dcore
dpkg-query -W -f='${binary:Package}\t${Version}\n' > /usr/share/dcore/packages.tsv
apt-cache policy incus > /usr/share/dcore/incus-origin.txt
apt-get clean
# DKMS's temporary build key must not survive in any container layer.
for directory in /var/lib/dkms /var/lib/shim-signed; do
    if [[ -d $directory ]]; then
        find "$directory" -type f \( -name '*.key' -o -name '*.priv' \) -delete
    fi
done
rm -rf /var/lib/apt/lists/*

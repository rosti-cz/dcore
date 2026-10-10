#!/usr/bin/env bash
# Adapted from bootcrew/mono/shared/initramfs.sh (Apache-2.0).
set -euo pipefail
install -d /usr/lib/dracut/dracut.conf.d
printf 'systemdsystemconfdir=/etc/systemd/system\nsystemdsystemunitdir=/usr/lib/systemd/system\n' > /usr/lib/dracut/dracut.conf.d/30-bootcrew-fix-bootc-module.conf
cat > /usr/lib/dracut/dracut.conf.d/30-bootcrew-bootc-container-build.conf <<'EOF'
reproducible=yes
hostonly=no
compress=zstd
add_dracutmodules+=" bootc ostree zfs "
EOF
mapfile -t kernels < <(find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
[[ ${#kernels[@]} == 1 ]]
kernel=${kernels[0]}
# Never allow dracut to select the builder's running kernel.
dracut --force --kver "$kernel" "/usr/lib/modules/${kernel}/initramfs.img"

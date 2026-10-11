#!/usr/bin/env bash
# shellcheck disable=SC2154 # dracut supplies initdir and systemdsystemunitdir.
# Grow dcore's inline LUKS2 root partition before systemd unlocks it.
check() { require_binaries /usr/bin/python3 growpart sgdisk sfdisk partx cryptsetup; }
depends() { echo "systemd-cryptsetup"; }
install() {
    local stdlib file
    inst_multiple /usr/bin/python3 /usr/libexec/dcore/grow-root \
        growpart sgdisk sfdisk partx cryptsetup blockdev lsblk timeout \
        awk sed grep cut cat head tail tr readlink mktemp mkdir rm rmdir flock stat date
    stdlib=$(/usr/bin/python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')
    # inst resolves Python extension dependencies and symlink targets too.
    while IFS= read -r -d '' file; do inst "$file"; done \
        < <(find "$stdlib" \( -type f -o -type l \) -print0)
    inst_multiple "$systemdsystemunitdir/systemd-udev-settle.service"
    inst_dir "$systemdsystemunitdir/systemd-cryptsetup@.service.d"
    cat > "$initdir$systemdsystemunitdir/dcore-grow-luks-root.service" <<'EOF'
[Unit]
Description=Grow the final LUKS2 root partition before unlocking
DefaultDependencies=no
Wants=systemd-udev-settle.service
After=systemd-udev-settle.service

[Service]
Type=oneshot
# A growth failure must never prevent normal password unlocking.
ExecStart=-/usr/bin/timeout 120 /usr/libexec/dcore/grow-root --initrd-luks-root dcore-root
RemainAfterExit=yes
EOF
    cat > "$initdir$systemdsystemunitdir/systemd-cryptsetup@.service.d/20-dcore-grow-root.conf" <<'EOF'
[Unit]
# Keep cryptsetup inactive until repartitioning and udev changes finish.
Wants=dcore-grow-luks-root.service
After=dcore-grow-luks-root.service
EOF
}

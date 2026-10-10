#!/usr/bin/env bash
set -euo pipefail
systemctl enable systemd-networkd systemd-resolved systemd-timesyncd ssh fstrim.timer
# Disable all competing image/APT update schedules, including upstream defaults.
for unit in dcore-update.timer bootc-fetch-apply-updates.timer bootc-fetch-updates.timer apt-daily.timer apt-daily-upgrade.timer; do
    if [[ -f "/usr/lib/systemd/system/$unit" ]]; then systemctl --root=/ disable "$unit"; fi
done
# Kernel modules are built in CI; DKMS cannot rebuild an immutable /usr at boot.
systemctl mask dkms.service
install -d -m 0755 /etc/netplan /etc/ssh/sshd_config.d
install -m 0600 /usr/share/dcore/default-network.yaml /etc/netplan/90-default.yaml
printf 'PasswordAuthentication no\n' > /etc/ssh/sshd_config.d/10-dcore.conf
# The key is unique to each installed server and must not be baked into an image.
rm -f /etc/ssh/ssh_host_*
: > /etc/machine-id
# Ensure sshd generates host keys on the first installed boot.
install -d /etc/systemd/system/ssh.service.d
printf '[Service]\nExecStartPre=/usr/bin/ssh-keygen -A\n' > /etc/systemd/system/ssh.service.d/10-host-keys.conf
# resolv-conf.conf creates the resolver symlink during systemd-tmpfiles at boot.
# Debian creates these accounts/groups with maintainer scripts. Declare them for
# installed hosts whose locally modified /etc is preserved across image updates.
install -d /usr/lib/sysusers.d
{
    for group in _ssh docker rdma; do
        if getent group "$group" > /dev/null; then printf 'g %s -\n' "$group"; fi
    done
    if getent passwd dnsmasq > /dev/null; then
        IFS=: read -r _ _ _ primary_gid _ user_home user_shell < <(getent passwd dnsmasq)
        primary_group=$(getent group "$primary_gid" | cut -d: -f1)
        printf 'u dnsmasq -:%s "DNS forwarder" %s %s\n' "$primary_group" "$user_home" "$user_shell"
    fi
} > /usr/lib/sysusers.d/10-dcore-extra.conf

#!/usr/bin/env bash
set -euo pipefail
# Both accounts already exist after the Homebrew build stage. Set the image's
# login defaults as well as the sysusers definition used for core provisioning.
usermod --shell /usr/bin/fish root
usermod --shell /usr/bin/fish core
systemctl enable systemd-networkd systemd-resolved systemd-timesyncd ssh fstrim.timer dcore-grow-root.service dcore-homebrew.service
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
printf '[Service]\nExecStartPre=\nExecStartPre=/usr/bin/ssh-keygen -A\nExecStartPre=/usr/sbin/sshd -t\n' > /etc/systemd/system/ssh.service.d/10-host-keys.conf
# resolv-conf.conf creates the resolver symlink during systemd-tmpfiles at boot.
# Debian creates these accounts/groups with maintainer scripts. Declare them for
# installed hosts whose locally modified /etc is preserved across image updates.
install -d /usr/lib/sysusers.d
{
    for group in _ssh docker rdma tss; do
        if getent group "$group" > /dev/null; then printf 'g %s -\n' "$group"; fi
    done
    if getent passwd dnsmasq > /dev/null; then
        IFS=: read -r _ _ _ primary_gid _ user_home user_shell < <(getent passwd dnsmasq)
        primary_group=$(getent group "$primary_gid" | cut -d: -f1)
        printf 'g %s -\n' "$primary_group"
        printf 'u dnsmasq -:%s "Service account" %s %s\n' "$primary_group" "$user_home" "$user_shell"
    fi
} > /usr/lib/sysusers.d/10-dcore-extra.conf

# dracut's TPM stack creates this account while generating the initramfs.
printf 'g tss -\nu tss -:tss "TPM software stack" /var/lib/tpm /bin/false\n' > /usr/lib/sysusers.d/10-dcore-tss.conf

# Debian's GPT-auto initrd root mount can fail with native composefs/systemd-boot.
# Let bootc emit the filesystem UUID so dracut explicitly mounts the root disk.
install -d -m 0755 /usr/lib/bootc/install
printf '[install]\ndiscoverable-partitions = false\n' > /usr/lib/bootc/install/10-dcore.toml
# Exercise bootc's TOML parser, not just the text written above.
bootc install print-configuration | python3 -c 'import json, sys; assert json.load(sys.stdin)["discoverable-partitions"] is False'

# Validate the SSH unit with the first-boot host-key drop-in applied.
systemd-analyze verify --man=no ssh.service

# Global environment for PAM, login/su, sudo, shell startup and systemd services.
homebrew_path=/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
python3 - "$homebrew_path" <<'PYPATH'
from pathlib import Path
import re, sys
value=sys.argv[1]
p=Path('/etc/environment')
s=p.read_text() if p.exists() else ''
s=re.sub(r'^PATH=.*\n?', '', s, flags=re.M)
p.write_text(s+'PATH="'+value+'"\n')
p=Path('/etc/login.defs'); s=p.read_text()
for field in ('ENV_PATH','ENV_SUPATH'):
    s=re.sub(r'^'+field+r'\s+.*$', field+' PATH='+value, s, flags=re.M)
p.write_text(s)
p=Path('/etc/bash.bashrc')
with p.open('a') as output: output.write('\n. /etc/profile.d/10-dcore-homebrew.sh\n')
# Sudo's last included Defaults overrides Debian's image-only secure_path.
p=Path('/etc/sudoers.d/10-dcore-homebrew')
p.write_text('Defaults secure_path="'+value+'"\ncore ALL=(ALL:ALL) NOPASSWD: ALL\n'); p.chmod(0o440)
p=Path('/usr/lib/systemd/system.conf.d/10-dcore-homebrew.conf')
p.parent.mkdir(parents=True,exist_ok=True)
p.write_text('[Manager]\nDefaultEnvironment="PATH='+value+'"\n')
p=Path('/usr/lib/environment.d/10-dcore-homebrew.conf')
p.parent.mkdir(parents=True,exist_ok=True)
p.write_text('PATH='+value+'\n')
PYPATH
visudo --check
# Switching is available for explicit configuration by the administrator.
systemctl --root=/ disable openvswitch-switch.service

# /var is empty on initial boot. Netplan's cleanup may activate this static DB
# unit even with the switching service disabled; ensure its symlink target exists.
install -d /usr/lib/systemd/system/ovsdb-server.service.d
printf 'd /var/lib/openvswitch 0755 root root -\nd /var/log/openvswitch 0755 root root -\n' > /usr/lib/tmpfiles.d/dcore-openvswitch.conf
printf '[Unit]\nAfter=systemd-tmpfiles-setup.service\n' > /usr/lib/systemd/system/ovsdb-server.service.d/10-persistent-state.conf

# Incus server activation is opt-in; the independently configured guest agent remains available.
systemctl --root=/ disable incus.service incus.socket incus-startup.service incus-lxcfs.service

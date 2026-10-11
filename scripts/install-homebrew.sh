#!/usr/bin/env bash
# Build the upstream installation once; boot copies it into persistent /var/home.
set -euo pipefail
systemd-sysusers /usr/lib/sysusers.d/10-dcore-core.conf
install -d -m 0700 -o core -g core /var/home/core
install -d -m 0755 -o core -g core /home/linuxbrew /home/linuxbrew/.linuxbrew
installer_commit=$(curl --proto '=https' --tlsv1.2 -fsSL https://api.github.com/repos/Homebrew/install/commits/HEAD | jq -er .sha)
[[ $installer_commit =~ ^[a-f0-9]{40}$ ]]
curl --proto '=https' --tlsv1.2 -fsSL "https://raw.githubusercontent.com/Homebrew/install/$installer_commit/install.sh" -o /tmp/homebrew-install.sh
chmod 0644 /tmp/homebrew-install.sh
runuser -u core -- env HOME=/var/home/core NONINTERACTIVE=1 HOMEBREW_NO_SUDO=1 \
    HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_AUTO_UPDATE=1 /bin/bash /tmp/homebrew-install.sh
# Include portable Ruby in the offline seed, not a first-login download.
runuser -u core -- env HOME=/var/home/core HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 \
    /home/linuxbrew/.linuxbrew/bin/brew --version
runuser -u core -- env HOME=/var/home/core HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 \
    HOMEBREW_FORCE_VENDOR_RUBY=1 /home/linuxbrew/.linuxbrew/bin/brew ruby -e 'puts RUBY_VERSION'
runuser -u core -- env HOME=/var/home/core HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 \
    /home/linuxbrew/.linuxbrew/bin/brew developer off
install -d /usr/share/dcore
printf 'installer=%s\nbrew=%s\n' "$installer_commit" \
    "$(runuser -u core -- git -C /home/linuxbrew/.linuxbrew/Homebrew rev-parse HEAD)" \
    > /usr/share/dcore/homebrew-seed-versions
# Ownership is applied to the actual core UID at boot, also after UID conflicts.
tar --create --zstd --owner=0 --group=0 --file /usr/share/dcore/homebrew-seed.tar.zst \
    --directory /home/linuxbrew/.linuxbrew .
rm -f /tmp/homebrew-install.sh

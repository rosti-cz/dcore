#!/usr/bin/env bash
# Exercise first boot, shared package commands, shell PATH and seed persistence.
set -euo pipefail
systemd-tmpfiles --create /usr/lib/tmpfiles.d/bootc-base-dirs.conf
install -d -m 0700 /root/.ssh
printf 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnlyInstallationSeed core-key-test\n' > /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys
install -d /etc/tmpfiles.d
: > /etc/tmpfiles.d/bootc-root-ssh.conf
/usr/libexec/dcore/init-homebrew
cmp /root/.ssh/authorized_keys /var/home/core/.ssh/authorized_keys
[[ $(stat -c '%U:%a' /var/home/core/.ssh/authorized_keys) == core:600 ]]
[[ $(stat -c %U /home/linuxbrew/.linuxbrew) == core ]]
[[ $(stat -c %U /home/linuxbrew/.linuxbrew/Homebrew/bin/brew) == core ]]
getent passwd core >/dev/null
grep -q '^core:' /etc/subuid
grep -q '^core:' /etc/subgid
# A prefix executable stands in for any formula installed later by core.
runuser -u core -- bash -c 'printf "#!/bin/sh\necho brew-path-visible\n" > /home/linuxbrew/.linuxbrew/bin/dcore-brew-path-check; chmod 755 /home/linuxbrew/.linuxbrew/bin/dcore-brew-path-check'
useradd --no-create-home --home-dir /var/home/brew-reader --shell /bin/bash brew-reader
install -d -m 0700 -o brew-reader -g brew-reader /var/home/brew-reader
for user in root core brew-reader; do
    for shell in /bin/bash /bin/dash; do
        runuser -u "$user" -- "$shell" -lc 'command -v brew; command -v dcore-brew-path-check; dcore-brew-path-check'
        runuser -u "$user" -- "$shell" -c 'command -v dcore-brew-path-check'
    done
    runuser -u "$user" -- fish -c 'command -v brew; command -v dcore-brew-path-check; dcore-brew-path-check'
done
runuser -u core -- sudo --non-interactive true
sudo -u core -H brew --version
sudo -u core -H dcore-brew-path-check
sudo dcore-brew-path-check
runuser -u core -- env HOME=/var/home/core HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_INSTALL_FROM_API=1 brew ruby -e 'puts RUBY_VERSION'
# A later boot must not replace a locally updated installation.
runuser -u core -- touch /home/linuxbrew/.linuxbrew/preserve-local-state
runuser -u core -- sh -c 'printf "locally-managed-key\n" > /var/home/core/.ssh/authorized_keys'
/usr/libexec/dcore/init-homebrew
grep -qx locally-managed-key /var/home/core/.ssh/authorized_keys
test -f /home/linuxbrew/.linuxbrew/preserve-local-state
test -x /home/linuxbrew/.linuxbrew/bin/dcore-brew-path-check
rm /home/linuxbrew/.linuxbrew/bin/dcore-brew-path-check
printf 'Core ownership, offline Ruby, shared shell PATH and persistence passed.\n'

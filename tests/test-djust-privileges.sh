#!/usr/bin/env bash
# Run only inside a disposable test container; command fixtures replace its tools.
set -euo pipefail
[[ -e /run/.containerenv ]] || { echo 'Use a disposable Podman test container.' >&2; exit 1; }
systemd-tmpfiles --create /usr/lib/tmpfiles.d/bootc-base-dirs.conf
install -d -m 0777 /run/djust-test
cat > /usr/bin/bootc <<'FIXTURE'
#!/bin/sh
printf 'bootc:%s:%s\n' "$(id -u)" "$*" >> /run/djust-test/calls
if [ "$1" = status ]; then
    printf '%s\n' '{"status":{"booted":{"image":{"image":{"image":"example/dcore:trixie"}}}}}'
fi
FIXTURE
cat > /usr/bin/brew <<'FIXTURE'
#!/bin/sh
printf 'brew:%s:%s\n' "$(id -u)" "$*" >> /run/djust-test/calls
[ "$(id -un)" = core ]
FIXTURE
chmod 0755 /usr/bin/bootc /usr/bin/brew
: > /run/djust-test/calls
chmod 0666 /run/djust-test/calls
if djust upgrade > /run/djust-test/root.log 2>&1; then echo 'Root djust was accepted' >&2; exit 1; fi
grep -q 'ordinary user' /run/djust-test/root.log
[[ ! -s /run/djust-test/calls ]]
runuser -u core -- djust upgrade
printf 'bootc:0:status --format=json\nbootc:0:upgrade\nbrew:1000:update\nbrew:1000:upgrade\n' > /run/djust-test/expected
cmp /run/djust-test/expected /run/djust-test/calls
: > /run/djust-test/calls
useradd --no-create-home --home-dir /var/home/djust-test-user djust-test-user
printf 'djust-test-user ALL=(ALL:ALL) NOPASSWD: ALL\n' > /etc/sudoers.d/djust-test-user
chmod 0440 /etc/sudoers.d/djust-test-user
runuser -u djust-test-user -- djust upgrade homebrew
printf 'brew:1000:update\nbrew:1000:upgrade\n' > /run/djust-test/expected
cmp /run/djust-test/expected /run/djust-test/calls
if runuser -u core -- sudo djust upgrade > /run/djust-test/sudo.log 2>&1; then echo 'sudo djust was accepted' >&2; exit 1; fi
grep -q 'ordinary user' /run/djust-test/sudo.log
printf 'Root rejected; OS elevated; Homebrew ran as core for both ordinary callers.\n'

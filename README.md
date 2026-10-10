# dcore

Debian Trixie bootc server image derived from [bootcrew/mono](https://github.com/bootcrew/mono/blob/main/debian/Containerfile).
It includes Debian ZFS (with a module compiled for the image kernel), Incus from
[Zabbly's stable repository](https://github.com/zabbly/incus), Docker and Compose,
all requested CLI tools, and the latest stable Rustic and Restic releases.
Podman supplies bootc's required container runtime; sudo is included for administration.
Release downloads are checked against upstream SHA-256 checksums.
`bat` and `ripgrep` provide Debian's `batcat` and `rg` commands.

## Installed tools

- Shell and terminal: fish, htop, vim, tmux, iotop, duf.
- Networking and APIs: nmap, wget, curl, HTTPie (`http`/`https`),
  iputils-tracepath (`tracepath`), rsync, rclone.
- Development and files: git, jq, unzip, bat (`batcat`), ripgrep (`rg`),
  bzip2, zstd.
- Containers: Incus (Zabbly stable), Docker (`docker.io` and `docker-cli`),
  Docker Compose (`docker compose`), Podman, skopeo.
- Backups: latest stable Rustic (`rustic`) and Restic (`restic`).
- System: bootc, OSTree, sudo, OpenSSH server, netplan, systemd-resolved,
  systemd-timesyncd (enabled), fstrim timer (enabled).
- Storage: Debian ZFS (`zfsutils-linux`, `zfs-zed`, `zfs-dracut`, and
  `zfs-dkms` compiled for the image kernel), Btrfs, ext4, XFS and FAT utilities.

The full package/version inventory is `/usr/share/dcore/packages.tsv` inside
an image. Backup-tool versions are recorded under `/usr/share/dcore` too.

## Build and publish

The [GitHub repository](https://github.com/rosti-cz/dcore) uses `main` as its
default branch. The workflow builds every day at 02:23 UTC, on pushes to main, on pull requests, and
on manual dispatch. Successful default-branch builds publish the same image to:

- `ghcr.io/rosti-cz/dcore:trixie`
- `ghcr.io/rosti-cz/dcore:stable`
- `ghcr.io/rosti-cz/dcore:latest`

The repository's `GITHUB_TOKEN` needs package write access (already requested by
the workflow). Set the GHCR package to public if hosts should pull anonymously;
otherwise configure registry credentials on each host. No publishing is done
for pull requests or manual builds on other branches. CI currently builds amd64;
the Containerfile also supports native arm64 builders.

`build-config.json` selects `releases`, `stable_release`, `bootc_ref`, and
`incus_channel`. Add a future codename to `releases` to build it alongside Trixie;
only `stable_release` receives `stable` and `latest`. Change `stable_release`
when ready to promote the new release. Zabbly must support that codename first,
and Debian package names and kernel/ZFS compatibility need validation.

Local build (Podman/Buildah recommended, no cache to refresh latest releases):

```sh
sudo podman build --pull=always --no-cache --format=oci \
  --build-arg DEBIAN_RELEASE=trixie \
  --build-arg BOOTC_REF=v1.17.1 \
  -t dcore:trixie -f Containerfile .
sudo podman run --rm dcore:trixie /usr/libexec/dcore/verify-image
```

`RUSTIC_VERSION` and `RESTIC_VERSION` default to `latest`, resolved during each
fresh build. Override them with release versions for reproducible builds.
bootc is pinned to v1.17.1. Trixie's libostree 2025.2 lacks composefs support
and is too old for current bootc. `shared/build-ostree.sh` builds pinned
OSTree 2026.4 and composefs 1.0.8 from checksum-verified upstream release
archives on Trixie, then installs them over the Debian boot-stack binaries.
Their versions and archive checksums should be updated together deliberately.
The image checks that its OSTree binary actually has composefs support.
ZFS and all requested distro tools remain Debian packages.

This creates a bootable OCI image. Use a bootc-compatible
installer/provisioning flow to install it, supply an administrator account and
SSH authorized key, and choose a persistent registry tag as the image source.
Password SSH login is disabled. Default networking uses DHCP on `en*` and `eth*`.
Replace `/etc/netplan/90-default.yaml` if your host needs static addressing.

## Test in an Incus VM

Use an **amd64** Incus host with VM support, an initialized storage pool, and a
profile providing network access (normally the default profile and `incusbr0`).
Make the `dcore` GHCR package public before using this anonymous-pull example.
The image name is `ghcr.io/rosti-cz/dcore:trixie`.

An empty VM cannot run an installer by itself. First use a temporary Debian VM
to install onto a new custom block volume, then attach that volume to an empty
VM. `bootc install to-disk` erases its target; the commands below select only the
new volume by its Incus device serial, rather than a guessed `/dev/sdX` name.
Run these commands in Bash on your Incus host:

```bash
POOL=default                         # change to your Incus storage pool
IMAGE=ghcr.io/rosti-cz/dcore:trixie
SSH_KEY="$HOME/.ssh/id_ed25519.pub"   # your existing public SSH key

test -f "$SSH_KEY" || { echo "Set SSH_KEY to an existing public key"; exit 1; }
incus storage list
incus launch images:debian/13 dcore-installer --vm -s "$POOL" \
  -c limits.cpu=2 -c limits.memory=4GiB -d root,size=20GiB
```

Wait until `incus exec dcore-installer -- true` succeeds (the Debian guest agent
needs time to start), then continue in the same shell:

```bash
incus storage volume create "$POOL" dcore-boot --type=block size=32GiB
incus config device add dcore-installer target disk \
  pool="$POOL" source=dcore-boot io.bus=virtio-blk
incus file push "$SSH_KEY" dcore-installer/root/dcore-test.pub
incus exec dcore-installer -- bash -eu -c \
  'apt-get update; apt-get install -y podman'
incus exec dcore-installer -- udevadm settle
incus exec dcore-installer -- lsblk -o NAME,SIZE,SERIAL,MOUNTPOINTS
incus exec dcore-installer -- test -b /dev/disk/by-id/virtio-incus_target
incus exec dcore-installer -- podman pull "$IMAGE"

incus exec dcore-installer -- podman run --rm --pull=never \
  --privileged --pid=host --ipc=host \
  --security-opt label=disable \
  -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
  -v /root/dcore-test.pub:/run/dcore-test.pub:ro \
  "$IMAGE" bootc install to-disk /dev/disk/by-id/virtio-incus_target \
  --filesystem ext4 --composefs-backend --bootloader systemd \
  --disable-selinux --generic-image --target-imgref "$IMAGE" \
  --root-ssh-authorized-keys /run/dcore-test.pub \
  --karg console=ttyS0,115200n8

incus stop dcore-installer
incus config device remove dcore-installer target
incus init dcore-test --empty --vm -s "$POOL" \
  -c limits.cpu=2 -c limits.memory=4GiB \
  -c security.secureboot=false -d root,size=1GiB
incus config device add dcore-test os disk \
  pool="$POOL" source=dcore-boot io.bus=virtio-blk boot.priority=10
incus start dcore-test --console
```

The 1 GiB root device satisfies Incus's instance layout; the VM boots from the
32 GiB `dcore-boot` volume. The installer VM must be stopped and the volume
detached before attaching it to the test VM. Do not attach a writable OS volume
to two running VMs. Exit the serial console with Ctrl+A, Q.

The explicit native composefs backend is required: bootc v1.17.1 supports
systemd-boot with that backend, while this image does not include GRUB/bootupd.
Secure Boot is disabled for this test because the image's ZFS module is unsigned.
The native backend is experimental; this is a validation recipe, not a claim
that a complete VM boot test has already passed.

Find the guest IP on your Incus bridge and connect with the private key matching
`SSH_KEY` (add `ssh -i /path/to/private-key` if needed):

```bash
incus network list-leases incusbr0
ssh root@GUEST_IP
```

Use SSH for dcore: it currently does not ship the Incus guest agent, so
`incus exec dcore-test` and guest-reported IP addresses in `incus list` are not
available by default. Inside the guest, check:

```sh
bootc status
uname -r
modprobe zfs
zfs version
incus version
docker compose version
rustic --version
restic version
timedatectl timesync-status
systemctl is-enabled systemd-timesyncd.service fstrim.timer dcore-update.timer
systemctl list-timers fstrim.timer
bootc upgrade                       # stage an available update without rebooting
# When ready to test the reboot:
# bootc upgrade --apply
```

Expected defaults: time synchronization and fstrim enabled, dcore updates
disabled. To test scheduled updates, use the timer instructions below. An image
built before the native-backend condition fix may skip the update units; check
`systemctl cat dcore-update.service dcore-update.timer`. Current units accept
either the OSTree marker or a native composefs kernel command-line parameter.

After shutting down the test guest, remove the test resources if desired:

```bash
incus stop dcore-test
incus delete dcore-test
incus delete dcore-installer
incus storage volume delete "$POOL" dcore-boot
```

See the official [Incus VM creation instructions](https://linuxcontainers.org/incus/docs/main/howto/instances_create/),
[block-volume devices](https://linuxcontainers.org/incus/docs/main/reference/devices_disk/),
and [bootc bootloader support](https://github.com/bootc-dev/bootc/blob/v1.17.1/docs/src/bootc-bootloaders.7.md).

## Weekly automatic image updates

Updates are disabled by default. Enable them on an installed host:

```sh
sudo systemctl enable --now dcore-update.timer
systemctl list-timers dcore-update.timer
```

Every Sunday at 04:00 local time, plus a random delay of up to one hour, the
service pulls the tag the host is already tracking using `bootc upgrade --apply`.
bootc stages a new image and reboots when an update is available. An unchanged
image does not cause a reboot. Missed runs are caught up after the next boot
(`Persistent=true`), so enabling the timer can trigger an immediate update.
Failed pulls do not trigger a reboot. The update includes the kernel and ZFS
module together; no runtime APT/DKMS rebuild is needed.

```sh
sudo systemctl disable --now dcore-update.timer
journalctl -u dcore-update.service
bootc status
```

Disable the timer before maintenance and let any already-running update finish.
To change the maintenance window, run `sudo systemctl edit dcore-update.timer`:

```ini
[Timer]
OnCalendar=
OnCalendar=Sat *-*-* 03:00:00
RandomizedDelaySec=30m
```

Then run `sudo systemctl daemon-reload` and `sudo systemctl restart dcore-update.timer`.
Use `sudo bootc switch <image>:<tag>` to change the tracked release. Host updates
replace the image; add/change packages in the Containerfile and rebuild.
The dpkg database lives under `/usr/lib/sysimage/dpkg`, with `/var/lib/dpkg`
linked to it, so package inspection and APT in derivative container builds work.
On installed hosts `/usr` is read-only; use bootc for system updates.

## Time, storage and containers

`systemd-timesyncd.service` and Debian's weekly `fstrim.timer` are enabled in the
image. Check after boot with `timedatectl timesync-status` and
`systemctl list-timers fstrim.timer`. fstrim discards unused blocks on supported
mounted filesystems. ZFS pool trimming is separate: opt in with
`sudo zpool set autotrim=on <pool>` if appropriate for the pool's storage.

ZFS is intended for data pools, including Incus storage. ZFS as the bootc root
filesystem is not configured. The image builds and checks its ZFS module before
publishing; Secure Boot requires a separate trusted module-signing setup.
Zabbly's `/opt/incus` payload is kept in `/usr/lib/opt/incus`, with `/opt` pointing
to `/usr/lib/opt` so its original paths continue to work and update atomically.
Use `/var/opt` for locally installed mutable software.
Initialize Incus on the installed host with `sudo incus admin init`.

Debian bootc here is a community integration. CI checks image structure,
maintenance defaults, kernel/ZFS artifacts, and tool executables; validate an
actual boot, time synchronization, networking, updates/reboot, and storage in a
VM before deploying to servers. See [UPSTREAM.md](UPSTREAM.md) for provenance.

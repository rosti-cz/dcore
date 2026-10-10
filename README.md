# dcore

Debian Trixie bootc server image derived from [bootcrew/mono](https://github.com/bootcrew/mono/blob/main/debian/Containerfile).
**dcore is still a work in progress and is not running anywhere in our production yet.**

**Secure Boot is not supported for now.** Disable it in the machine's UEFI
firmware or VM configuration before booting dcore. The image does not configure
a trusted Secure Boot chain or provide an enrollable dcore signing certificate
for its build-time ZFS modules. Secure Boot signing and key enrollment are
deferred.

It includes Debian ZFS (with a module compiled for the image kernel), Incus from
[Zabbly's stable repository](https://github.com/zabbly/incus), Docker and Compose,
all requested CLI tools, and the latest stable Rustic and Restic releases.
Podman supplies bootc's required container runtime; sudo is included for administration.
Release downloads are checked against upstream SHA-256 checksums.
`bat` and `ripgrep` provide Debian's `batcat` and `rg` commands.

## Installed tools

- Shell and terminal: fish, htop, vim, tmux, iotop, ncdu, duf.
- Networking and APIs: nmap, wget, curl, HTTPie (`http`/`https`),
  iputils-tracepath (`tracepath`), rsync, rclone.
- Development and files: git, jq, unzip, bat (`batcat`), ripgrep (`rg`),
  bzip2, zstd.
- Containers: Incus (Zabbly stable), Incus guest-agent integration (Debian), Docker (`docker.io` and `docker-cli`),
  Docker Compose (`docker compose`), Podman, skopeo.
- Backups: latest stable Rustic (`rustic`) and Restic (`restic`).
- System: bootc, OSTree, sudo, OpenSSH server, netplan, systemd-resolved,
  systemd-timesyncd (enabled), fstrim timer (enabled).
- Storage: Debian ZFS (`zfsutils-linux`, `zfs-zed`, `zfs-dracut`, and
  `zfs-dkms` compiled for the image kernel), parted, `btrfs-progs` (`btrfs`), ext4, XFS and FAT utilities.

The full package/version inventory is `/usr/share/dcore/packages.tsv` inside
an image. Backup-tool versions are recorded under `/usr/share/dcore` too.

## Build and publish

The [GitHub repository](https://github.com/rosti-cz/dcore) uses `main` as its
default branch. The workflow builds every day at 02:23 UTC, on pushes to main, on pull requests, and
on manual dispatch. Successful default-branch builds publish the same image to:

- `ghcr.io/rosti-cz/dcore:trixie`
- `ghcr.io/rosti-cz/dcore:stable`
- `ghcr.io/rosti-cz/dcore:latest`

These daily tags describe **Debian releases**, not the maturity of the dcore
project. `trixie` is Debian's codename; `stable` follows the Debian release
selected by `stable_release`; `latest` is an alias for that same default stable
Debian build. None of these tags imply that dcore is production-ready. Versioned
OCI tags created for GitHub releases identify dcore project releases separately.

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

## Testing options

Use an amd64 test machine and wired DHCP networking. Incus and QEMU need an
x86_64 VM with UEFI firmware and Secure Boot disabled; bare metal needs the same
firmware configuration. These images use native composefs, systemd-boot and an
ext4 root filesystem. The Incus guest agent supplies access inside Incus. QEMU
and bare metal need your SSH public key provisioned before their first boot;
password login and a default administrator password are not provided.

- [Incus](#test-in-an-incus-vm): install the current OCI image with the helper,
  or import a release image.
- [QEMU](#test-with-qemu): build a disk with your key, or boot a provisioned
  release disk with a writable overlay.
- [Bare metal](#test-on-bare-metal): install the OCI image from live Linux,
  or write a provisioned raw release image onto a spare disk.
- [System checks](#verify-the-installed-system): verify bootc, networking,
  services, storage and updates after boot.

## Test in an Incus VM

The installer automates the temporary-VM procedure below. Run it on an amd64
Incus host with VM support, an initialized storage pool, and a profile with
network access:

```bash
./scripts/incus_install dcore-test --pool default --ssh-key ~/.ssh/id_ed25519.pub
# Optional settings:
./scripts/incus_install dcore-other --image ghcr.io/rosti-cz/dcore:trixie \
  --profile default --cpu 4 --memory 8GiB --disk 48GiB
incus exec dcore-test -- bash
incus list dcore-test
incus exec dcore-test -- bootc status
```

The SSH public key is optional: the Incus guest agent provides administrator
access. The script refuses existing resource names and never uses a host disk.
It checks the new OS volume's virtio serial, installs with bootc, then waits up
to ten minutes for the dcore agent. It removes its temporary installer on success;
on failure it prints available serial-console logs and removes resources it
created. Pass `--keep-on-failure` to preserve failed VMs and volumes for inspection.
The custom OS volume persists on success; delete it separately when
removing the VM. Use `--help` for all options. OCI mode needs a publicly pullable
image; it does not copy host registry credentials into the guest.

For a published release, use its actual tag:

```bash
./scripts/incus_install dcore-release --release v0.1.0 --debian trixie \
  --pool default --ssh-key ~/.ssh/id_ed25519.pub
incus exec dcore-release -- bash
```

If the agent wait times out, use `--keep-on-failure` and inspect
`incus console INSTANCE --show-log`. Confirm the Incus host can supply its VM
agent (Debian-packaged hosts may need `incus-agent` installed on the host too).
After testing an OCI installation, remove its VM and separate OS volume:

```bash
incus stop dcore-test
incus delete dcore-test
incus storage volume delete default dcore-test-boot
```

A release import uses the instance's root disk, so deleting `dcore-release`
also removes that disk. The installer caches imported images in Incus.

The guest-agent setup is triggered by Incus's virtio port through Debian's udev
rule. It copies the host's agent and per-instance credentials into `/run`, then
runs that host-matched binary. No agent credentials are baked into the image and
no writes to immutable `/usr` are required. A regular QEMU VM without the Incus
port does not start the agent.

### Manual installation

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
incus storage volume create "$POOL" dcore-boot --type=block size=10GiB
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
10 GiB `dcore-boot` volume. The installer VM must be stopped and the volume
detached before attaching it to the test VM. Do not attach a writable OS volume
to two running VMs. Exit the serial console with Ctrl+A, Q.

The explicit native composefs backend is required: bootc v1.17.1 supports
systemd-boot with that backend, while this image does not include GRUB/bootupd.
The image's install configuration disables GPT-auto root discovery so bootc
writes an explicit root filesystem UUID into boot parameters. This avoids the
[upstream GPT-auto initrd mount issue](https://github.com/bootc-dev/bootc/issues/2184)
and applies to both OCI installs and release disks.
Secure Boot must remain disabled because dcore does not configure a trusted
boot chain or trusted signatures for its ZFS modules.
The native backend is experimental. A local QEMU boot and SSH smoke test passed
with the corrected image; the automated Incus install/import and guest-agent
protocol have not yet been tested against an Incus daemon.

Find the guest IP on your Incus bridge and connect with the private key matching
`SSH_KEY` (add `ssh -i /path/to/private-key` if needed):

```bash
incus network list-leases incusbr0
ssh root@GUEST_IP
```

Current builds include the Incus guest agent, so `incus exec dcore-test -- bash`
and guest-reported addresses in `incus list` are available after boot. Run the
[shared system checks](#verify-the-installed-system) inside the guest.

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

## GitHub release images

Publishing a GitHub release triggers a separate workflow that checks out the
release's tag, builds every configured Debian variant for amd64, publishes a
versioned OCI image, and installs that exact image digest into a fresh disk. It
does not move the daily `trixie`, `stable`, or `latest` tags. The installed disk
tracks the corresponding Debian codename tag for future `bootc upgrade` calls.
The release manifest records the initial source digest and commit.

Each release contains the following (replace `v0.1.0` with the actual tag):

- `dcore-v0.1.0-trixie-amd64.raw.xz`: compressed whole-disk raw image, including
  the partition table and UEFI bootloader (10 GiB when decompressed).
- `dcore-v0.1.0-trixie-amd64.qcow2`: standalone UEFI QEMU disk.
- `dcore-v0.1.0-trixie-amd64-incus.tar.xz`: unified Incus VM image, including
  metadata and the same QCOW2 disk.
- Per-variant provenance manifest, plus combined `SHA256SUMS`.

Download artifacts only after the release workflow finishes. The examples use
`v0.1.0` as a placeholder; substitute a tag from the
[releases page](https://github.com/rosti-cz/dcore/releases). Run the following in
Bash in a new download directory. Each download verifies only its matching
checksum entry, so you do not need to fetch every release asset:

```bash
set -euo pipefail
RELEASE=v0.1.0
STEM="dcore-${RELEASE}-trixie-amd64"
RELEASE_URL="https://github.com/rosti-cz/dcore/releases/download/${RELEASE}"
curl --fail --location --retry 3 "$RELEASE_URL/SHA256SUMS" -o SHA256SUMS

download_asset() {
  curl --fail --location --retry 3 "$RELEASE_URL/$1" -o "$1" || return
  awk -v file="$1" '$2 == file || $2 == "*"file' SHA256SUMS |
    sha256sum --check --strict -
}
```

For a manual Incus import, use the variables and function above:

```bash
download_asset "$STEM-incus.tar.xz" && \
  incus image import "$STEM-incus.tar.xz" --alias "dcore-$RELEASE"
incus launch "dcore-$RELEASE" dcore-release --vm -s default \
  -c limits.cpu=2 -c limits.memory=4GiB -c security.secureboot=false
# Wait up to ten minutes for the guest agent:
timeout 600 bash -c 'until incus exec dcore-release -- true >/dev/null 2>&1; do sleep 3; done'
incus exec dcore-release -- bash
```

The published artifacts contain no personal SSH keys or passwords. In Incus,
provision a key through the agent if you also want SSH access:

```bash
incus exec dcore-release -- install -d -m 0700 /root/.ssh
incus file push ~/.ssh/id_ed25519.pub \
  dcore-release/root/.ssh/authorized_keys --mode=0600
```

## Prepare a release disk with SSH access

Use this procedure before testing a **published** disk in QEMU or on bare metal.
It modifies your downloaded copy; release artifacts and their original checksums
remain unchanged. You need Linux, sudo, Podman, util-linux (`losetup`, `lsblk`),
xz and an existing SSH public key. Only the public key is copied into the disk.
Keep the private key on the machine from which you will SSH.

Using the release download variables/function above:

```bash
download_asset "$STEM.raw.xz" && xz --decompress --keep "$STEM.raw.xz"
RAW_IMAGE="$(realpath "$STEM.raw")"
SSH_KEY="$(realpath ~/.ssh/id_ed25519.pub)"
```

Alternatively, download the QCOW2 artifact and convert it to a raw working copy
with `qemu-utils` installed:

```bash
download_asset "$STEM.qcow2" && \
  qemu-img convert -f qcow2 -O raw "$STEM.qcow2" "$STEM.raw"
RAW_IMAGE="$(realpath "$STEM.raw")"
SSH_KEY="$(realpath ~/.ssh/id_ed25519.pub)"
```

Choose one path. The raw disk is 10 GiB logically; decompression/conversion and
copies require sufficient local storage. The current bootc layout has a BIOS
placeholder partition, an EFI system partition, then ext4 root as partition 3.
Mount it through bootc so that writes reach the deployment's persistent `/var`,
rather than attempting to edit its immutable root image directly:

```bash
(
  set -euo pipefail
  test -f "$RAW_IMAGE"
  test -f "$SSH_KEY"
  IMAGE=ghcr.io/rosti-cz/dcore:trixie
  sudo podman pull "$IMAGE"
  WORK_DIR=$(mktemp -d)
  LOOP_DEVICE=
  cleanup() {
    if [[ -n "$LOOP_DEVICE" ]]; then sudo losetup --detach "$LOOP_DEVICE"; fi
    rmdir "$WORK_DIR/deployment" "$WORK_DIR/sysroot" "$WORK_DIR" 2>/dev/null || true
  }
  trap cleanup EXIT
  mkdir "$WORK_DIR/sysroot" "$WORK_DIR/deployment"
  LOOP_DEVICE=$(sudo losetup --find --show --partscan "$RAW_IMAGE")
  sudo udevadm settle
  sudo lsblk -o NAME,SIZE,FSTYPE,PARTLABEL "$LOOP_DEVICE"
  # Confirm partition 3 is the ext4 root partition shown above.
  sudo podman run --rm --privileged --security-opt label=disable \
    -v /dev:/dev -v "$WORK_DIR:/work" \
    -v "$SSH_KEY:/run/dcore-key.pub:ro" \
    -e "ROOT_PARTITION=${LOOP_DEVICE}p3" "$IMAGE" bash -eu -c '
      trap '\''umount -R /work/deployment 2>/dev/null || true; umount /work/sysroot 2>/dev/null || true'\'' EXIT
      mount "$ROOT_PARTITION" /work/sysroot
      bootc install mount --sysroot /work/sysroot --latest /work/deployment
      install -d -m 0700 /work/deployment/root/.ssh
      install -m 0600 /run/dcore-key.pub /work/deployment/root/.ssh/authorized_keys
      sync
    '
)
```

Do this only while the disk is not booted or attached to a running VM. The key
file above replaces `authorized_keys` in your working disk. For old releases,
use a matching versioned OCI image such as
`ghcr.io/rosti-cz/dcore:v0.1.0-trixie` if its bootc version differs from `:trixie`.
See [bootc deployment mounting](https://bootc.dev/bootc/bootc-installation.7.html#postprocessing-after-to-filesystem).

## Test with QEMU

On an amd64 Debian/Ubuntu Linux host, install the host tools:

```bash
sudo apt-get update
sudo apt-get install -y podman skopeo qemu-system-x86 qemu-utils ovmf xz-utils
```

### Build a local disk from the current OCI image

Run this from the cloned dcore repository with an existing public key. `skopeo`
resolves the current immutable image digest; the disk tracks `:trixie` for later
updates. Use a fresh output directory if you already built `local-test`:

```bash
IMAGE=ghcr.io/rosti-cz/dcore:trixie
DIGEST=$(skopeo inspect --format '{{.Digest}}' "docker://$IMAGE")
sudo env SSH_PUBLIC_KEY="$(realpath ~/.ssh/id_ed25519.pub)" \
  scripts/build-disk-images.sh \
  "ghcr.io/rosti-cz/dcore@$DIGEST" "$IMAGE" local-test trixie ./dist
BASE_DISK="$(realpath dist/dcore-local-test-trixie-amd64.qcow2)"
BASE_FORMAT=qcow2
```

### Use a published release disk

Follow [release disk key provisioning](#prepare-a-release-disk-with-ssh-access)
first, then select its raw disk as the backing file:

```bash
BASE_DISK="$RAW_IMAGE"
BASE_FORMAT=raw
```

### Boot the test VM

Create a writable overlay so the prepared base disk stays unchanged, and use a
private UEFI variable store for each VM:

```bash
qemu-img create -f qcow2 -F "$BASE_FORMAT" -b "$BASE_DISK" dcore-test.qcow2
cp /usr/share/OVMF/OVMF_VARS_4M.fd dcore-test-vars.fd
qemu-system-x86_64 -enable-kvm -machine q35 -m 4096 -smp 2 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=dcore-test-vars.fd \
  -drive if=virtio,format=qcow2,file=dcore-test.qcow2 \
  -nic user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:2222-:22 \
  -nographic
```

Firmware paths vary by distribution. Use a non-Secure-Boot OVMF code image and
its matching variable-store size. KVM requires `/dev/kvm` access. For a slower
software-only test, replace `-enable-kvm` with `-accel tcg`. The serial console
is `ttyS0`; Ctrl+A, X terminates QEMU, so shut the guest down first.

Connect from another terminal with the private key corresponding to the public
key you provisioned:

```bash
ssh -i ~/.ssh/id_ed25519 -p 2222 root@127.0.0.1
```

Run the [shared checks](#verify-the-installed-system), then test a reboot and
reconnect. For subsequent boots, rerun QEMU with the existing overlay and
variable store; do not recreate them. If testing a different VM on the same
SSH port, remove its previous host-key entry only after confirming it is your
replacement test VM: `ssh-keygen -R '[127.0.0.1]:2222'`.

When finished, run `poweroff` inside the guest. After QEMU exits, remove only the
writable test files if desired:

```bash
rm -- dcore-test.qcow2 dcore-test-vars.fd
```

See [QEMU's drive and networking options](https://www.qemu.org/docs/master/system/invocation.html).

## Test on bare metal

Use a spare amd64 machine/disk. Enable UEFI boot and disable Secure Boot in its
firmware. The image does not provide a legacy BIOS bootloader, despite the
placeholder GPT BIOS partition. Use Ethernet with DHCP for the first boot;
wireless networking and additional non-free device firmware are not configured.
Have an existing public SSH key available on the live environment (for example
on another USB drive). The private key stays on your administration machine.

### Install the current OCI image from live Linux

Boot a Debian/Ubuntu live USB **in UEFI mode** with working internet access.
Podman needs several GiB of scratch space for the image. Install the live tools:

```bash
sudo apt-get update
sudo apt-get install -y podman
```

Identify the destination by its model, serial and size. Use a whole-disk
`/dev/disk/by-id/` path, excluding `-partN` paths, and make sure none of its
partitions are mounted or used as swap. These commands erase the entire selected
disk; select the spare disk rather than your live USB or an existing system disk.

```bash
lsblk -o NAME,PATH,SIZE,MODEL,SERIAL,MOUNTPOINTS
ls -l /dev/disk/by-id/
```

Set the two paths below explicitly. The confirmation must match the complete
destination path:

```bash
(
  set -euo pipefail
  IMAGE=ghcr.io/rosti-cz/dcore:trixie
  TARGET_DISK=/dev/disk/by-id/REPLACE_WITH_YOUR_SPARE_DISK
  SSH_KEY="$(realpath /path/to/your/id_ed25519.pub)"
  test -d /sys/firmware/efi
  test -b "$TARGET_DISK"
  test -f "$SSH_KEY"
  lsblk -o NAME,SIZE,MODEL,SERIAL,MOUNTPOINTS "$TARGET_DISK"
  read -r -p "Type ERASE $TARGET_DISK to install: " CONFIRM
  [[ "$CONFIRM" == "ERASE $TARGET_DISK" ]]
  sudo podman pull "$IMAGE"
  sudo podman run --rm --privileged --pid=host --ipc=host \
    --security-opt label=disable \
    -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
    -v "$SSH_KEY:/run/dcore-install.pub:ro" \
    "$IMAGE" bootc install to-disk "$TARGET_DISK" \
    --filesystem ext4 --composefs-backend --bootloader systemd \
    --disable-selinux --generic-image --target-imgref "$IMAGE" \
    --root-ssh-authorized-keys /run/dcore-install.pub --karg console=tty0
  sync
)
```

This creates a disk layout using the destination's capacity. `--generic-image`
installs the standard UEFI fallback loader without changing the live system's
firmware boot entries. Shut down, remove the live USB, and choose the destination
disk in the firmware boot menu. If the firmware needs an explicit EFI file,
select `EFI/BOOT/BOOTX64.EFI` on that disk's EFI partition.

### Write a raw release image to a spare disk

First download, verify, decompress and provision your public key using
[the release-disk procedure](#prepare-a-release-disk-with-ssh-access). You can do
that on your Linux workstation before copying the resulting `.raw` file to the
live environment. A checksum of the original compressed release artifact no
longer describes your key-customized raw file.

In live Linux, install the GPT tool and identify the destination as above:

```bash
sudo apt-get update
sudo apt-get install -y gdisk
```

The destination must be at least the size of the decompressed raw image. Writing
a disk file with `dd` erases existing partitions and data on that whole disk:

```bash
(
  set -euo pipefail
  RAW_IMAGE="$(realpath /path/to/key-provisioned-dcore.raw)"
  TARGET_DISK=/dev/disk/by-id/REPLACE_WITH_YOUR_SPARE_DISK
  test -f "$RAW_IMAGE"
  test -b "$TARGET_DISK"
  [[ $(sudo blockdev --getsize64 "$TARGET_DISK") -ge $(stat -c %s "$RAW_IMAGE") ]]
  lsblk -o NAME,SIZE,MODEL,SERIAL,MOUNTPOINTS "$TARGET_DISK"
  read -r -p "Type ERASE $TARGET_DISK to write the image: " CONFIRM
  [[ "$CONFIRM" == "ERASE $TARGET_DISK" ]]
  sudo dd if="$RAW_IMAGE" of="$TARGET_DISK" bs=16M conv=fsync status=progress
  # Place the backup GPT at the actual end when the target exceeds 10 GiB.
  sudo sgdisk --move-second-header "$TARGET_DISK"
  sync
)
```

Shut down, remove the live USB, and boot the destination in UEFI mode. This path
starts with the image's 10 GiB partition layout. On boot, automatic root growth
extends the final root partition and filesystem to use the larger disk;
relocating the backup GPT alone does not grow the filesystem. No bare-metal
hardware boot has been validated by this project yet.

Find the server's DHCP address in your router, then connect from your
administration machine:

```bash
ssh -i ~/.ssh/id_ed25519 root@SERVER_IP
```

The Incus guest agent does not start on bare metal; your SSH key provides
administrator access. Run the shared checks below before relying on the machine.

## Verify the installed system

Run these as root through `incus exec`, SSH, or a configured console account:

```bash
bootc status
uname -r
ip -br address
networkctl status
systemctl --failed
modprobe zfs
zfs version
incus version
docker compose version
rustic --version
restic version
btrfs --version
parted --version
ncdu --version
systemctl is-active ssh.service systemd-timesyncd.service docker.service
systemctl is-enabled fstrim.timer dcore-grow-root.service
systemctl is-enabled dcore-update.timer || true
timedatectl timesync-status
timedatectl show -p NTPSynchronized
systemctl list-timers fstrim.timer
```

Expected: working DHCP/SSH, ZFS module matching the running kernel, active time
service, fstrim enabled, and `dcore-update.timer` disabled. The time service being
active does not prove synchronization; wait for `NTPSynchronized=yes` when an
NTP server is reachable. For failures, inspect `journalctl -b` and the relevant
unit's journal. An unchanged OCI image does not exercise an update/reboot.

To test manual image updates, wait for a newer daily image to be published:

```bash
bootc upgrade          # stage the newer tracked image without rebooting
bootc status
# When ready to apply it and deliberately reboot:
bootc upgrade --apply
```

Reconnect and inspect `bootc status` and `uname -r`. Use the
[weekly timer instructions](#weekly-automatic-image-updates) below to opt in to
scheduled rebooting updates. Keep that timer disabled while testing storage.
For an ordinary reboot test, run `systemctl reboot` and reconnect afterward.

## Validation status

Release publication is the trigger; committing the workflow does not create a
release. Local validation exercised bootc disk installation, compressed QCOW2
integrity, Incus archive structure, checksums, and provenance generation. An
unmodified build then booted and rebooted successfully under QEMU with SSH
access and a persistent SSH host key, kernel 6.12.111, ZFS 2.3.9, active
`systemd-timesyncd` and Docker, fstrim enabled,
automatic updates disabled, and no failed systemd units. External NTP
synchronization was not confirmed. Incus daemon import,
`incus exec`/guest-agent protocol, and the actual GitHub release event/upload
remain unverified. Validate these before relying on the release pipeline.

Root-growth validation additionally booted 10 GiB native bootc disks under QEMU
with ext4, single-device Btrfs and XFS. All three had over 4 GiB free, expanded
successfully when their virtual disks grew to 14 GiB and then 16 GiB, and stayed
unchanged across an intervening reboot. User data, SSH host keys, and read-only
`/usr` were preserved. Integration tests also cover interrupted growth,
read-only backing mounts, a root followed by another partition, unusual
partition numbering, multi-device Btrfs and unsupported layouts. These tests
run in daily builds and PR checks, and block publishing if they fail.

## Automatic root disk growth

New release disks and the Incus installer default to **10 GiB**. Older release
artifacts may contain larger disks; pass a matching `--disk` size when importing
them (for example `--disk 32GiB` for an older 32 GiB image). The release format
continues to use ext4; automatic growth also supports installed Btrfs and XFS
roots. Select those filesystems with `bootc install to-disk --filesystem btrfs`
or `--filesystem xfs --allow-missing-verity` when doing a custom OCI
installation. XFS does not provide fs-verity with our kernel; the explicit
allow-missing-verity option matches the tested native composefs installation. A direct OCI install
normally fills its destination immediately; release disks can start smaller
than their destination device.

`dcore-grow-root.service` is enabled by default and runs on every boot. It finds
the backing filesystem at `/sysroot`, checks its GPT table, and grows root only
when it is the **physically last partition** with trailing unused space. It
preserves partition starts, identifiers, and all other partitions. Reserved GPT
sectors and insignificant alignment gaps remain unused. The filesystem is also
checked for growth when its partition was already enlarged, so an interrupted
attempt can recover on the next boot.

- ext4 uses `resize2fs`.
- Single-device Btrfs uses `btrfs filesystem resize DEVICE_ID:max`.
- XFS uses `xfs_growfs -d`.

The commands run in a private mount namespace against the physical backing
filesystem. The host's existing mount flags and read-only `/usr` stay intact.
Containers, whole-device filesystems, non-GPT tables, encryption/device-mapper,
LVM, RAID, multi-device Btrfs, and a root followed by another partition are
skipped. Errors are logged as a failed service; this service is not required
for normal boot and never deliberately reboots the machine.

Inspect the backing mount, partition sizes and growth log after boot:

```bash
findmnt /sysroot
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
journalctl -b -u dcore-grow-root.service
systemctl status dcore-grow-root.service
sudo /usr/libexec/dcore/grow-root --dry-run
```

To reserve trailing disk space for another purpose, mask the service **before**
booting the disk on a larger device. On an installed system:

```bash
sudo systemctl mask dcore-grow-root.service
# Re-enable and retry later:
sudo systemctl unmask dcore-grow-root.service
sudo systemctl enable dcore-grow-root.service
sudo systemctl restart dcore-grow-root.service
```

For an offline disk, during the SSH-key provisioning mount procedure above,
create `/work/deployment/etc/systemd/system/dcore-grow-root.service` as a symlink
to `/dev/null` before unmounting the deployment. This prevents its first-boot
growth too.

To test expansion under QEMU, stop the VM and enlarge its writable overlay:

```bash
qemu-img resize dcore-test.qcow2 20G
```

Then rerun QEMU and inspect the growth log and `df -h /sysroot`. A further disk
expansion should be picked up after the next boot. Do not run `qemu-img resize`
while QEMU is using the image. With an imported Incus release, expand the root
volume, then reboot:

```bash
incus config device override dcore-release root size=20GiB
incus restart dcore-release
```

If `root` was already overridden locally, use
`incus config device set dcore-release root size=20GiB` instead. For an OCI
installation created by our helper, its OS disk is a separate custom volume:

```bash
incus stop dcore-test
incus storage volume set default dcore-test-boot size=20GiB
incus start dcore-test
```

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
publishing; trusted module signing and key enrollment are not configured, so
Secure Boot must remain disabled.
Zabbly's `/opt/incus` payload is kept in `/usr/lib/opt/incus`, with `/opt` pointing
to `/usr/lib/opt` so its original paths continue to work and update atomically.
Use `/var/opt` for locally installed mutable software.
Initialize Incus on the installed host with `sudo incus admin init`.

Debian bootc here is a community integration. CI checks image structure,
maintenance defaults, kernel/ZFS artifacts, and tool executables; validate an
actual boot, time synchronization, networking, updates/reboot, and storage in a
VM before deploying to servers. See [UPSTREAM.md](UPSTREAM.md) for provenance.

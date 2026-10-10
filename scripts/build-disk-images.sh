#!/usr/bin/env bash
# Build generic UEFI raw, QEMU and Incus images from one exact OCI image.
set -euo pipefail

usage() {
    echo "Usage: $0 IMAGE@sha256:DIGEST TRACKING_IMAGE RELEASE_TAG DEBIAN_RELEASE OUTPUT_DIR [DISK_SIZE=10G]"
    echo "Optional local customization: SSH_PUBLIC_KEY=/absolute/path/to/key.pub"
}
[[ ${1:-} != --help ]] || { usage; exit 0; }
[[ $# -ge 5 && $# -le 6 ]] || { usage >&2; exit 2; }
source_image=$1
tracking_image=$2
release_tag=$3
debian_release=$4
output_dir=$5
disk_size=${6:-10G}
[[ $EUID -eq 0 ]] || { echo 'Run this builder as root (privileged installation is required).' >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'Only amd64 disk images are supported.' >&2; exit 1; }
[[ $source_image =~ ^[a-z0-9./:_-]+@sha256:[a-f0-9]{64}$ ]] || { echo 'Source must be a registry image pinned by SHA256 digest.' >&2; exit 2; }
[[ $tracking_image =~ ^[a-z0-9./_-]+:[a-zA-Z0-9._-]+$ ]] || { echo 'Invalid tracking image reference.' >&2; exit 2; }
[[ $release_tag =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]] || { echo 'Release tags must be filename-safe (letters, numbers, dot, underscore, dash; max 80 characters).' >&2; exit 2; }
[[ $debian_release =~ ^[a-z][a-z0-9-]*$ ]] || { echo 'Invalid Debian release.' >&2; exit 2; }
[[ $disk_size =~ ^[1-9][0-9]*G$ ]] || { echo 'Disk size must be a whole number of GiB, e.g. 10G.' >&2; exit 2; }
for tool in podman qemu-img python3 tar xz sha256sum truncate; do
    command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done
podman_args=()
install_args=()
ssh_key_configured=false
if [[ -n ${SSH_PUBLIC_KEY:-} ]]; then
    [[ -f $SSH_PUBLIC_KEY ]] || { echo 'SSH_PUBLIC_KEY must name an existing public key file.' >&2; exit 2; }
    command -v ssh-keygen >/dev/null || { echo 'ssh-keygen is required to validate SSH_PUBLIC_KEY.' >&2; exit 1; }
    ssh-keygen -l -f "$SSH_PUBLIC_KEY" >/dev/null || { echo 'Invalid SSH public key.' >&2; exit 2; }
    # Explicitly reject private key material, even if ssh-keygen can read it.
    python3 - "$SSH_PUBLIC_KEY" <<'PYKEY'
import pathlib, sys
lines = pathlib.Path(sys.argv[1]).read_text().splitlines()
assert lines and all(line.startswith(('ssh-', 'ecdsa-', 'sk-')) for line in lines if line.strip()), 'Supply public keys only'
PYKEY
    podman_args=(-v "$(realpath "$SSH_PUBLIC_KEY"):/run/dcore-authorized-keys:ro")
    install_args=(--root-ssh-authorized-keys /run/dcore-authorized-keys)
    ssh_key_configured=true
fi
mkdir -p "$output_dir"
output_dir=$(realpath "$output_dir")
stem="dcore-${release_tag}-${debian_release}-amd64"
for suffix in .raw.xz .qcow2 -incus.tar.xz -manifest.json -SHA256SUMS; do
    [[ ! -e "$output_dir/$stem$suffix" ]] || { echo "Refusing to overwrite $output_dir/$stem$suffix" >&2; exit 1; }
done
# Keep uncompressed raw disks temporary; publish only the compressed raw image.
work_dir=$(mktemp -d "$output_dir/.disk-build.XXXXXXXX")
trap 'rm -rf -- "$work_dir"' EXIT
truncate -s "$disk_size" "$work_dir/root.raw"
podman pull "$source_image"
# This is a newly created regular file, never a host block device. bootc manages
# its loop device and partitions; a separate mount namespace contains mounts.
podman run --rm --privileged --pid=host --ipc=host \
    --security-opt label=disable \
    -v /var/lib/containers:/var/lib/containers \
    -v /dev:/dev \
    -v "$work_dir:/output" \
    "${podman_args[@]}" "$source_image" bootc install to-disk \
    --via-loopback --composefs-backend --bootloader systemd \
    --filesystem ext4 --disable-selinux --generic-image \
    --source-imgref "docker://$source_image" \
    --target-imgref "$tracking_image" \
    --karg=console=tty0 --karg=console=ttyS0,115200n8 \
    "${install_args[@]}" /output/root.raw
qemu-img convert -c -f raw -O qcow2 "$work_dir/root.raw" "$output_dir/$stem.qcow2"
qemu-img check "$output_dir/$stem.qcow2"
# Compress the original whole-disk image directly, including its partition table.
# Bound compression memory/threads on hosted runners, and verify the stream.
xz --threads=2 -3 --stdout "$work_dir/root.raw" > "$output_dir/$stem.raw.xz"
xz --test "$output_dir/$stem.raw.xz"
# Hard-link instead of copying the potentially large disk for Incus packaging.
ln "$output_dir/$stem.qcow2" "$work_dir/rootfs.img"
creation_date=$(date +%s)
python3 - "$work_dir/metadata.yaml" "$release_tag" "$debian_release" "$creation_date" <<'PYMETADATA'
import json, sys
path, version, release, created = sys.argv[1:]
# JSON is a YAML subset; quoting prevents release names from changing structure.
metadata = {'architecture': 'x86_64', 'creation_date': int(created), 'properties': {
    'os': 'Debian', 'release': release, 'variant': 'dcore', 'version': version,
    'description': f'dcore {version} Debian {release} amd64 (UEFI, Secure Boot disabled)',
    'requirements.secureboot': 'false'}}
with open(path, 'w') as output:
    json.dump(metadata, output, indent=2)
    output.write('\n')
PYMETADATA
# Limit xz memory and threads for hosted runners. QCOW2 remains standalone.
XZ_OPT='-T2 -3' tar --create --xz --file "$output_dir/$stem-incus.tar.xz" \
    --directory "$work_dir" metadata.yaml rootfs.img
podman run --rm "$source_image" cat /usr/share/dcore/packages.tsv > "$work_dir/packages.tsv"
podman run --rm "$source_image" cat /usr/share/dcore/backup-versions > "$work_dir/backup-versions"
podman run --rm "$source_image" cat /usr/share/dcore/boot-stack-versions > "$work_dir/boot-stack-versions"
podman run --rm "$source_image" bootc --version > "$work_dir/bootc-version"
podman run --rm "$source_image" cat /usr/share/dcore/bootc-commit > "$work_dir/bootc-commit"
podman image inspect "$source_image" > "$work_dir/image.json"
python3 - "$work_dir" "$output_dir/$stem-manifest.json" "$source_image" "$tracking_image" "$release_tag" "$debian_release" "$creation_date" "$disk_size" "$ssh_key_configured" <<'PYMANIFEST'
import json, pathlib, sys
work, output, source, tracking, version, release, created, size, ssh_key = sys.argv[1:]
work = pathlib.Path(work)
image = json.loads((work / 'image.json').read_text())[0]
packages = []
for line in (work / 'packages.tsv').read_text().splitlines():
    fields = line.split('\t')
    packages.append({'package': fields[0], 'version': fields[1]})
versions = dict(line.split('=', 1) for name in ('backup-versions', 'boot-stack-versions')
                for line in (work / name).read_text().splitlines())
manifest = {'release': version, 'debian_release': release, 'architecture': 'amd64',
            'created': int(created), 'source_image': source, 'tracking_image': tracking,
            'source_revision': image.get('Labels', {}).get('org.opencontainers.image.revision'),
            'disk_size': size, 'bootloader': 'systemd-boot', 'backend': 'composefs',
            'bootc_version': (work / 'bootc-version').read_text().strip(),
            'bootc_commit': (work / 'bootc-commit').read_text().strip(),
            'secure_boot': False, 'root_ssh_key_configured': ssh_key == 'true', 'versions': versions, 'packages': packages}
with open(output, 'w') as stream:
    json.dump(manifest, stream, indent=2)
    stream.write('\n')
PYMANIFEST
(
    cd "$output_dir"
    sha256sum "$stem.raw.xz" "$stem.qcow2" "$stem-incus.tar.xz" "$stem-manifest.json" > "$stem-SHA256SUMS"
)
# GitHub release assets must be smaller than 2 GiB each.
for artifact in "$output_dir/$stem.raw.xz" "$output_dir/$stem.qcow2" "$output_dir/$stem-incus.tar.xz"; do
    [[ $(stat -c %s "$artifact") -lt 2147483648 ]] || { echo "Artifact exceeds GitHub's 2 GiB asset limit: $artifact" >&2; exit 1; }
done
echo "Built release artifacts in $output_dir"

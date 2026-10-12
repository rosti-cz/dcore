#!/usr/bin/env bash
# Build an offline Debian live ISO containing one exact release OCI image.
set -euo pipefail
usage() {
    echo "Usage: $0 IMAGE@sha256:DIGEST TRACKING_IMAGE RELEASE_TAG DEBIAN_RELEASE OUTPUT_DIR"
}
[[ ${1:-} != --help ]] || { usage; exit 0; }
[[ $# == 5 ]] || { usage >&2; exit 2; }
source_image=$1; tracking_image=$2; release_tag=$3; debian_release=$4
[[ $EUID == 0 && $(uname -m) == x86_64 ]] || { echo 'Requires an amd64 root builder.' >&2; exit 1; }
[[ $source_image =~ ^[a-z0-9./:_-]+@sha256:[a-f0-9]{64}$ ]] || { echo 'Source must be pinned by digest.' >&2; exit 2; }
[[ $tracking_image =~ ^[a-z0-9./_-]+:[a-zA-Z0-9._-]+$ ]] || { echo 'Invalid update reference.' >&2; exit 2; }
[[ $release_tag =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ && $debian_release =~ ^[a-z][a-z0-9-]*$ ]] || { echo 'Invalid release name.' >&2; exit 2; }
for tool in podman gzip python3 sha256sum; do command -v "$tool" >/dev/null; done
mkdir -p "$5"
output_dir=$(realpath "$5")
repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
stem="dcore-$release_tag-$debian_release-amd64-installer"
work_dir=$(mktemp -d "$output_dir/.iso-build-XXXXXX")
# The live-build chroot only exists inside the disposable builder container.
trap 'rm -rf -- "$work_dir"' EXIT
local_image="localhost/dcore-installer:$release_tag-$debian_release"
podman image exists "$source_image" || podman pull "$source_image"
podman tag "$source_image" "$local_image"
mkdir -p "$work_dir/payload"
cp -r "$repo_dir/scripts" "$work_dir/source"
cp "$repo_dir/rootfs/usr/libexec/dcore/enroll-secureboot" "$work_dir/source/iso/enroll-secureboot"
podman run --rm "$local_image" cat /usr/share/dcore/secureboot/dcore.der > "$work_dir/payload/dcore.der"
podman run --rm "$local_image" cat /usr/share/dcore/secureboot/status.json > "$work_dir/payload/secureboot-status.json"
# Preserve the manifest digest so installed hosts can fetch this exact version later.
podman save --format oci-archive --uncompressed "$local_image" | gzip -6 > "$work_dir/payload/image.oci.tar.gz"
image_size=$(podman image inspect "$local_image" --format '{{.Size}}')
python3 - "$work_dir/payload/installer.json" "$source_image" "$tracking_image" "$local_image" "$release_tag" "$debian_release" "$image_size" <<'PY'
import json, sys, tarfile
from pathlib import Path
path, source, tracking, local, release, debian, image_size = sys.argv[1:]
image_size = int(image_size)
signing = json.loads(Path(path).with_name('secureboot-status.json').read_text())
assert signing['signed'] is True, 'Release installer requires signed dcore modules'
with tarfile.open(Path(path).with_name('image.oci.tar.gz'), mode='r|gz') as archive:
    for entry in archive:
        if entry.name.lstrip('./') == 'index.json':
            index = json.load(archive.extractfile(entry))
            assert any(item['digest'] == source.split('@', 1)[1] for item in index['manifests']), 'OCI export changed the source image digest'
            break
    else:
        raise ValueError('OCI archive index is missing')
gib = 1024**3
# Cover temporary archive extraction and image storage on the 75% RAM mount.
minimum_ram = max(16 * gib, ((2 * image_size + gib) * 4 // 3 + gib + 4 * gib - 1) // (4 * gib) * (4 * gib))
with open(path, 'w') as output:
    json.dump(dict(source_image=source, tracking_image=tracking, local_image=local,
                   release=release, debian_release=debian, architecture='amd64',
                   image_size_bytes=image_size, minimum_ram_bytes=minimum_ram,
                   default_filesystem='ext4', filesystems=['ext4', 'btrfs', 'xfs'],
                   encryption_modes=['none', 'luks2'], secure_boot=True,
                   secure_boot_certificate_sha256=signing['certificate_sha256'],
                   secure_boot_enrollment='MOK for ZFS modules'), output, indent=2)
    output.write('\n')
PY
podman run --rm --privileged --security-opt label=disable \
    -v "$work_dir/source:/source:ro" -v "$work_dir:/work" \
    -e "DEBIAN_RELEASE=$debian_release" docker.io/library/debian:trixie \
    bash /source/iso/build-live.sh
install -m 0644 "$work_dir/live-image-amd64.hybrid.iso" "$output_dir/$stem.iso"
install -m 0644 "$work_dir/payload/installer.json" "$output_dir/$stem-manifest.json"
[[ $(stat -c %s "$output_dir/$stem.iso") -lt 2147483648 ]] || {
    echo 'Installer ISO exceeds the GitHub release asset limit of 2 GiB.' >&2; exit 1;
}
(
    cd "$output_dir"
    sha256sum "$stem.iso" "$stem-manifest.json" > "$stem-SHA256SUMS"
)
echo "Built offline installer: $output_dir/$stem.iso"

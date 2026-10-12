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
podman save --format oci-archive "$local_image" | gzip -6 > "$work_dir/payload/image.oci.tar.gz"
python3 - "$work_dir/payload/installer.json" "$source_image" "$tracking_image" "$local_image" "$release_tag" "$debian_release" <<'PY'
import json, sys
path, source, tracking, local, release, debian = sys.argv[1:]
with open(path, 'w') as output:
    json.dump(dict(source_image=source, tracking_image=tracking, local_image=local,
                   release=release, debian_release=debian, architecture='amd64',
                   filesystem='ext4', encryption=False, secure_boot=False), output, indent=2)
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

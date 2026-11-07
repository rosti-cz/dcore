#!/usr/bin/env bash
# Repack an existing image without changing it. Run as root with Podman.
set -euo pipefail
[[ $# == 2 && $(id -u) == 0 && $2 == *-chunked ]] || {
    echo 'Usage (as root): build-chunked-image.sh SOURCE_IMAGE TARGET_IMAGE-chunked' >&2
    exit 2
}
source_image=$1
target_image=$2
[[ $source_image != "$target_image" ]] || { echo 'Source and target must differ.' >&2; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# chunkah v0.7.1, pinned so CI cannot silently change the layer packing algorithm.
chunkah_image=quay.io/coreos/chunkah@sha256:71e593421c5de7dcc7c8c5c50e98d3bac572055a76f399f61a491d123c20a523
work=$(mktemp -d "${TMPDIR:-/var/tmp}/dcore-chunked.XXXXXX")
container=''
verification_container=''
cleanup() {
    if [[ -n $verification_container ]]; then
        podman unmount "$verification_container" >/dev/null 2>&1 || true
        podman rm "$verification_container" >/dev/null
    fi
    if [[ -n $container ]]; then
        podman unmount "$container" >/dev/null 2>&1 || true
        podman rm "$container" >/dev/null
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT
podman image inspect "$source_image" > "$work/source.json"
# Mount a fresh writable container; copy-up keeps the source image untouched.
container=$(podman create --entrypoint /bin/true "$source_image")
rootfs=$(podman mount "$container")
python3 "$script_dir/chunk-components.py" "$rootfs"
podman run --rm --network=none --security-opt label=disable \
    -v "$rootfs:/chunkah:ro" -v "$work:/out:rw" \
    "$chunkah_image" build --config /out/source.json \
    --source-date-epoch 0 --max-layers 128 --threads 2 \
    --tag "$target_image" --output /out/image.oci
podman load --input "$work/image.oci"
podman image inspect "$target_image" > "$work/chunked.json"
python3 - "$work/source.json" "$work/chunked.json" <<'PY'
import json, sys
source, chunked = (json.load(open(name))[0] for name in sys.argv[1:])
assert source['Config'] == chunked['Config'], 'Rechunking changed the image configuration'
layers = chunked['RootFS']['Layers']
assert 1 < len(layers) <= 128, f'Expected chunked layers, got {len(layers)}'
print(f'Chunked image validated: {len(layers)} layers; image configuration preserved')
PY
verification_container=$(podman create --entrypoint /bin/true "$target_image")
verification_rootfs=$(podman mount "$verification_container")
python3 "$script_dir/verify-chunked-rootfs.py" "$rootfs" "$verification_rootfs"
podman run --rm --network=none "$target_image" /usr/libexec/dcore/verify-image

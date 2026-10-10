#!/usr/bin/env bash
set -euo pipefail
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
fetch() { curl --proto '=https' --tlsv1.2 --retry 3 -fsSL "$1" -o "$2"; }
release() {
    local repo=$1 version=$2
    if [[ "$version" == latest ]]; then
        fetch "https://api.github.com/repos/${repo}/releases/latest" release.json
        jq -er '.tag_name' release.json
    else
        printf 'v%s\n' "${version#v}"
    fi
}
case "$(dpkg --print-architecture)" in
    amd64) rustic_arch=x86_64; restic_arch=amd64 ;;
    arm64) rustic_arch=aarch64; restic_arch=arm64 ;;
    *) exit 1 ;;
esac
rustic_tag=$(release rustic-rs/rustic "$RUSTIC_VERSION")
asset="rustic-${rustic_tag}-${rustic_arch}-unknown-linux-gnu.tar.gz"
url="https://github.com/rustic-rs/rustic/releases/download/${rustic_tag}"
fetch "$url/$asset" "$asset"
fetch "$url/$asset.sha256" "$asset.sha256"
# Rustic currently publishes a bare digest; also accept sha256sum format.
digest=$(awk 'NR == 1 { print $1 }' "$asset.sha256")
[[ "$digest" =~ ^[[:xdigit:]]{64}$ ]]
printf '%s  %s\n' "$digest" "$asset" | sha256sum --check --strict
mkdir rustic
# Never install into /usr/local: bootc makes it persistent under /var.
tar -xzf "$asset" -C rustic
mapfile -t binaries < <(find rustic -type f -name rustic)
[[ ${#binaries[@]} == 1 ]]
install -m 0755 "${binaries[0]}" /usr/bin/rustic
restic_tag=$(release restic/restic "$RESTIC_VERSION")
asset="restic_${restic_tag#v}_linux_${restic_arch}.bz2"
url="https://github.com/restic/restic/releases/download/${restic_tag}"
fetch "$url/$asset" "$asset"
fetch "$url/SHA256SUMS" SHA256SUMS
awk -v name="$asset" '$2 == name || $2 == "*" name { print }' SHA256SUMS > selected.sha256
[[ $(wc -l < selected.sha256) == 1 ]]
sha256sum --check --strict selected.sha256
bzip2 -dc "$asset" > restic
install -m 0755 restic /usr/bin/restic
rustic --version
restic version
printf 'rustic=%s\nrestic=%s\n' "$rustic_tag" "$restic_tag" > /usr/share/dcore/backup-versions

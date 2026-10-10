#!/usr/bin/env bash
# Trixie's libostree is too old for current bootc and lacks composefs support.
set -euo pipefail
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
fetch() { curl --proto '=https' --tlsv1.2 --retry 3 -fsSL "$1" -o "$2"; }
composefs_version=1.0.8
ostree_version=2026.4
fetch "https://github.com/composefs/composefs/releases/download/v${composefs_version}/composefs-${composefs_version}.tar.xz" composefs.tar.xz
printf '%s  composefs.tar.xz\n' 207384deb196198ac4764c5b42bb558f7c661494302b380afc09447678538386 | sha256sum --check --strict
fetch "https://github.com/ostreedev/ostree/releases/download/v${ostree_version}/libostree-${ostree_version}.tar.xz" ostree.tar.xz
printf '%s  ostree.tar.xz\n' b26c9016eb03bb4ee52cc00c642d56e00fc79ae7faac6bf4aa317d7451339ef7 | sha256sum --check --strict
tar -xf composefs.tar.xz
tar -xf ostree.tar.xz
libdir="/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)"
meson setup composefs-build "composefs-${composefs_version}" \
    --prefix=/usr --libdir="$libdir" --buildtype=release -Dman=disabled -Dfuse=disabled
meson compile -C composefs-build -j 2
meson install -C composefs-build
DESTDIR=/output meson install -C composefs-build
ldconfig
cd "libostree-${ostree_version}"
./configure --prefix=/usr --sysconfdir=/etc --libdir="$libdir" --libexecdir=/usr/libexec \
    --disable-gtk-doc --disable-introspection --disable-man \
    --with-curl --without-soup --without-soup3 --without-avahi \
    --with-crypto=openssl --with-composefs --with-dracut \
    --with-systemdsystemunitdir=/usr/lib/systemd/system \
    --with-systemdsystemgeneratordir=/usr/lib/systemd/system-generators
make -j 2
make install
make install DESTDIR=/output
ldconfig
ostree --version | tee /tmp/dcore-ostree-version
# Fail now rather than publish an image that cannot mount its root at boot.
grep -q 'composefs' /tmp/dcore-ostree-version
install -d /output/usr/share/dcore /output/usr/share/licenses/ostree /output/usr/share/licenses/composefs
install -m 0644 /tmp/dcore-ostree-version /output/usr/share/dcore/ostree-version
cp COPYING /output/usr/share/licenses/ostree/
cp ../composefs-${composefs_version}/COPYING* ../composefs-${composefs_version}/LICENSE* /output/usr/share/licenses/composefs/
printf 'ostree=%s\ncomposefs=%s\n' "$ostree_version" "$composefs_version" > /output/usr/share/dcore/boot-stack-versions
# The runtime image does not need development headers or static libraries.
rm -rf /output/usr/include /output/usr/lib/pkgconfig
find /output/usr/lib -type f \( -name '*.a' -o -name '*.la' -o -name '*.pc' \) -delete

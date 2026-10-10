#!/usr/bin/env bash
# Adapted from bootcrew/mono/shared/build.sh (Apache-2.0).
set -euo pipefail
git clone --depth 1 --branch "$BOOTC_REF" https://github.com/bootc-dev/bootc.git .
make bin install-all DESTDIR=/output CARGO_OPTIONS=--locked
install -d /output/usr/share/dcore
git rev-parse HEAD > /output/usr/share/dcore/bootc-commit

#!/usr/bin/env bash
# Builds a .deb using dh-virtualenv — installs to /opt/venvs/icloudz/.
# Output: ../../dist/icloudz_<version>-1_<arch>.deb
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEBIAN_SRC="$(dirname "$0")/debian"

echo "Building icloudz — dh-virtualenv"

# Place debian/ at source root (required by dpkg-buildpackage)
cp -r "$DEBIAN_SRC" "$ROOT/debian"

cleanup() { rm -rf "$ROOT/debian"; }
trap cleanup EXIT

cd "$ROOT"
dpkg-buildpackage -us -uc -b 2>&1

# dpkg-buildpackage writes the .deb one level above $ROOT
DEB=$(ls "${ROOT}/../icloudz_"*.deb 2>/dev/null | sort -V | tail -1)
if [ -n "$DEB" ]; then
    mv "$DEB" "$ROOT/dist/"
    # also move .changes / .buildinfo if present
    ls "${ROOT}/../icloudz_"*.changes "${ROOT}/../icloudz_"*.buildinfo 2>/dev/null \
        | xargs -I{} mv {} "$ROOT/dist/" 2>/dev/null || true
    echo "Done: dist/$(basename "$DEB")"
else
    echo "Build succeeded but .deb not found — check above output"
fi

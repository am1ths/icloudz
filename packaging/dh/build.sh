#!/usr/bin/env bash
# Builds a .deb using dh-virtualenv — installs to /opt/venvs/icloudz/.
# Output: ../../dist/icloudz_<version>-1_<arch>.deb
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEBIAN_SRC="$(dirname "$0")/debian"

echo "Building icloudz — dh-virtualenv"

# Place debian/ at source root (required by dpkg-buildpackage)
if [ -e "$ROOT/debian" ]; then
    echo "Refusing to overwrite existing $ROOT/debian" >&2
    exit 1
fi
cleanup() { rm -rf "$ROOT/debian"; }
trap cleanup EXIT
cp -r "$DEBIAN_SRC" "$ROOT/debian"

cd "$ROOT"
dpkg-buildpackage -us -uc -b 2>&1

# dpkg-buildpackage writes the .deb one level above $ROOT
shopt -s nullglob
DEBS=("${ROOT}/../icloudz_"*.deb)
if [ "${#DEBS[@]}" -gt 0 ]; then
    DEB=$(printf '%s\n' "${DEBS[@]}" | sort -V | tail -1)
    mkdir -p "$ROOT/dist"
    mv "$DEB" "$ROOT/dist/"
    # also move .changes / .buildinfo if present
    ls "${ROOT}/../icloudz_"*.changes "${ROOT}/../icloudz_"*.buildinfo 2>/dev/null \
        | xargs -I{} mv {} "$ROOT/dist/" 2>/dev/null || true
    echo "Done: dist/$(basename "$DEB")"
else
    echo "Build succeeded but .deb not found — check above output"
fi

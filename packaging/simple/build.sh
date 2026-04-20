#!/usr/bin/env bash
# Builds a .deb with a bundled virtualenv — no dh-virtualenv needed.
# Output: ../../dist/icloudz_<version>_amd64.deb
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PKG=icloudz
VERSION=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT')
import tomllib
with open('$ROOT/pyproject.toml', 'rb') as f:
    print(tomllib.load(f)['project']['version'])
")
ARCH=$(dpkg --print-architecture)
STAGE=$(mktemp -d)
PKG_DIR="$STAGE/${PKG}_${VERSION}_${ARCH}"

echo "Building $PKG $VERSION ($ARCH) — simple venv bundle"

# ── Directory tree ────────────────────────────────────────────────────────────
mkdir -p "$PKG_DIR/opt/icloudz"
mkdir -p "$PKG_DIR/usr/bin"
mkdir -p "$PKG_DIR/DEBIAN"

# ── Virtualenv ────────────────────────────────────────────────────────────────
python3 -m venv "$PKG_DIR/opt/icloudz/venv"
"$PKG_DIR/opt/icloudz/venv/bin/pip" install --quiet --upgrade pip
"$PKG_DIR/opt/icloudz/venv/bin/pip" install --quiet "$ROOT"

# Strip __pycache__ and .dist-info bloat
find "$PKG_DIR/opt/icloudz/venv" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# ── Wrapper ───────────────────────────────────────────────────────────────────
cat > "$PKG_DIR/usr/bin/icloudz" << 'EOF'
#!/bin/sh
exec /opt/icloudz/venv/bin/icloudz "$@"
EOF
chmod 755 "$PKG_DIR/usr/bin/icloudz"

# ── DEBIAN/control ────────────────────────────────────────────────────────────
INSTALLED_KB=$(du -sk "$PKG_DIR/opt" "$PKG_DIR/usr" | awk '{s+=$1} END{print s}')
cat > "$PKG_DIR/DEBIAN/control" << EOF
Package: $PKG
Version: $VERSION
Architecture: $ARCH
Maintainer: Timofei Shabalin <timshab@icloud.com>
Installed-Size: $INSTALLED_KB
Depends: python3 (>= 3.10)
Section: utils
Priority: optional
Description: iCloud Drive sync daemon for Linux
 Bidirectional sync with a background daemon, watchdog for instant
 local uploads, and systemd integration.
EOF

# ── Build ─────────────────────────────────────────────────────────────────────
OUT="$ROOT/dist/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$PKG_DIR" "$OUT"
rm -rf "$STAGE"
echo "Done: $OUT"

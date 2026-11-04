#!/usr/bin/env bash
# Download the Xray release pinned in node-image/versions.lock into DEST_DIR for tests (CI, a
# developer machine, the panel server): the same URL and the same three sha256 checks as
# provision.sh. DEST_DIR gets "xray" and "geoip.dat"; nothing is installed or executed.
#   node-image/fetch-xray.sh DEST_DIR
# Then: PROXYHUB_TEST_XRAY=DEST_DIR/xray go test ./... (agent) or pytest (panel).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOCK="$HERE/versions.lock"
[ $# -eq 1 ] || { echo "usage: $0 DEST_DIR" >&2; exit 2; }
DEST="$1"

lock() {
  local line
  line="$(grep -E "^$1=[A-Za-z0-9._:/+-]+$" "$LOCK")" || { echo "versions.lock: missing $1" >&2; exit 1; }
  printf '%s\n' "${line#*=}"
}

check_sha256() {  # check_sha256 FILE EXPECTED
  local got
  got="$(sha256sum "$1" | cut -d' ' -f1)"
  if [ "$got" != "$2" ]; then
    echo "sha256 mismatch for $1: refusing it" >&2
    exit 1
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$WORK/xray.zip" "$(lock XRAY_URL)"
check_sha256 "$WORK/xray.zip" "$(lock XCORE_ZIP_SHA256)"
mkdir "$WORK/x"
unzip -q -o "$WORK/xray.zip" xray geoip.dat -d "$WORK/x"
check_sha256 "$WORK/x/xray" "$(lock XCORE_BINARY_SHA256)"
check_sha256 "$WORK/x/geoip.dat" "$(lock XCORE_GEOIP_SHA256)"
mkdir -p "$DEST"
install -m 0755 "$WORK/x/xray" "$DEST/xray"
install -m 0644 "$WORK/x/geoip.dat" "$DEST/geoip.dat"
echo "xray $(lock XRAY_VERSION) in $DEST"

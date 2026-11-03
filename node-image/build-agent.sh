#!/usr/bin/env bash
# Reproducible build of the node agent for linux/amd64, pinned by node-image/versions.lock.
# Same source + same Go toolchain + these flags = the same bytes, so AGENT_SHA256 in versions.lock
# identifies the binary exactly. Prints "<sha256>  <path>".
# Usage: node-image/build-agent.sh OUTPUT_FILE
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
LOCK="$HERE/versions.lock"
[ $# -eq 1 ] || { echo "usage: $0 OUTPUT_FILE" >&2; exit 2; }
OUT="$1"

lock() {
  local line
  line="$(grep -E "^$1=[A-Za-z0-9._:/+-]+$" "$LOCK")" || { echo "versions.lock: missing $1" >&2; exit 1; }
  printf '%s\n' "${line#*=}"
}

AGENT_VERSION="$(lock AGENT_VERSION)"
AGENT_GO_VERSION="$(lock AGENT_GO_VERSION)"

mkdir -p "$(dirname "$OUT")"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
cd "$ROOT/agent"
env -u GOFLAGS GOTOOLCHAIN="go$AGENT_GO_VERSION" CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -mod=readonly -trimpath -buildvcs=false \
  -ldflags "-s -w -buildid= -X main.version=$AGENT_VERSION" \
  -o "$OUT" ./cmd/agent
sha256sum "$OUT"

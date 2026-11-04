#!/usr/bin/env bash
# Provision a Proxy-Hub node (Telemt MTProto + Xray VLESS Reality) on Ubuntu 24.04 (docs/10 M2.2,
# M2.4, docs/11). Run as root, either
# while building the node image (Packer) or from cloud-init on a fresh server. Idempotent.
#
#   provision.sh (--agent-binary FILE | --agent-url https://AGENT_DOMAIN/agent/v1/artifacts/proxyhub-agent) [--start]
#
# Installs Telemt and Xray (pinned URL + sha256 from versions.lock) and the agent binary (sha256
# pinned too; with --agent-url it is downloaded from the panel's agent site first), and hardens the
# host:
#   * nftables: outbound 25/465/587, private/special ranges and 169.254.169.254 dropped;
#     inbound 22, Telemt's local metrics/API ports and Xray's API port dropped; 443 (Xray) and
#     8443 (Telemt) open;
#   * no SSH server; journald in memory only; rsyslog removed; no core dumps;
#   * Telemt's and Xray's stdout/stderr to /dev/null, their configs rendered only by the agent;
#   * BBR congestion control with the fq qdisc.
# --start also applies everything now (cloud-init mode); without it, it takes effect at next boot
# (image build mode). The agent itself starts once cloud-init has written /etc/proxyhub-agent/.
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "$0")" && pwd)"
LOCK="$HERE/versions.lock"
FILES="$HERE/files"
AGENT_BINARY=""
AGENT_URL=""
START=0

usage() { echo "usage: $0 (--agent-binary FILE | --agent-url HTTPS_URL) [--start]" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --agent-binary) [ $# -ge 2 ] || usage; AGENT_BINARY="$2"; shift 2 ;;
    --agent-url) [ $# -ge 2 ] || usage; AGENT_URL="$2"; shift 2 ;;
    --start) START=1; shift ;;
    *) usage ;;
  esac
done
if [ -n "$AGENT_BINARY" ] && [ -n "$AGENT_URL" ]; then usage; fi
if [ -z "$AGENT_BINARY" ] && [ -z "$AGENT_URL" ]; then usage; fi
if [ -n "$AGENT_URL" ]; then
  case "$AGENT_URL" in
    https://*/agent/v1/artifacts/proxyhub-agent) ;;
    *) echo "--agent-url must be https://<agent domain>/agent/v1/artifacts/proxyhub-agent" >&2; exit 2 ;;
  esac
fi
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
# shellcheck source=/dev/null
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then echo "Ubuntu 24.04 only" >&2; exit 1; fi
[ "$(uname -m)" = "x86_64" ] || { echo "x86_64 only (versions.lock pins the x86_64 build)" >&2; exit 1; }

lock() {
  local line
  line="$(grep -E "^$1=[A-Za-z0-9._:/+-]+$" "$LOCK")" || { echo "versions.lock: missing $1" >&2; exit 1; }
  printf '%s\n' "${line#*=}"
}

check_sha256() {  # check_sha256 FILE EXPECTED
  local got
  got="$(sha256sum "$1" | cut -d' ' -f1)"
  if [ "$got" != "$2" ]; then
    echo "sha256 mismatch for $1: refusing to install" >&2
    exit 1
  fi
}

export DEBIAN_FRONTEND=noninteractive

echo "== packages"
# On first boot apt-daily / unattended-upgrades may hold the dpkg lock: wait for it (up to 10
# minutes) instead of failing, and retry a failed index update a few times.
APT=(apt-get -o DPkg::Lock::Timeout=600)
updated=0
for attempt in 1 2 3 4 5; do
  if "${APT[@]}" update -q; then updated=1; break; fi
  echo "apt-get update failed (attempt $attempt), retrying" >&2
  sleep $((attempt * 10))
done
if [ "$updated" -ne 1 ]; then
  echo "apt-get update kept failing: refusing to continue" >&2
  exit 1
fi
"${APT[@]}" install -y -q --no-install-recommends nftables curl ca-certificates unzip
"${APT[@]}" purge -y -q openssh-server rsyslog || true
"${APT[@]}" autoremove -y -q
systemctl mask ssh.service ssh.socket >/dev/null 2>&1 || true

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== telemt $(lock TELEMT_VERSION)"
curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$WORK/telemt.tar.gz" "$(lock TELEMT_URL)"
check_sha256 "$WORK/telemt.tar.gz" "$(lock TELEMT_TARBALL_SHA256)"
mkdir "$WORK/x"
tar -xzf "$WORK/telemt.tar.gz" -C "$WORK/x" --no-same-owner telemt
check_sha256 "$WORK/x/telemt" "$(lock TELEMT_BINARY_SHA256)"
install -o root -g root -m 0755 "$WORK/x/telemt" /usr/local/bin/telemt

echo "== xray $(lock XRAY_VERSION)"
curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$WORK/xray.zip" "$(lock XRAY_URL)"
check_sha256 "$WORK/xray.zip" "$(lock XCORE_ZIP_SHA256)"
mkdir "$WORK/xr"
# Only the binary and geoip.dat (for the geoip:private routing block); never anything else.
unzip -q -o "$WORK/xray.zip" xray geoip.dat -d "$WORK/xr"
check_sha256 "$WORK/xr/xray" "$(lock XCORE_BINARY_SHA256)"
check_sha256 "$WORK/xr/geoip.dat" "$(lock XCORE_GEOIP_SHA256)"
install -o root -g root -m 0755 "$WORK/xr/xray" /usr/local/bin/xray
install -d -o root -g root -m 0755 /usr/local/share/xray
install -o root -g root -m 0644 "$WORK/xr/geoip.dat" /usr/local/share/xray/geoip.dat

echo "== proxyhub-agent $(lock AGENT_VERSION)"
if [ -n "$AGENT_URL" ]; then
  # From the panel's agent site (public ACME certificate). Integrity comes from AGENT_SHA256 in
  # versions.lock, which reached this node inside its user data, not from the download itself.
  AGENT_BINARY="$WORK/proxyhub-agent"
  curl -fsSL --proto '=https' --tlsv1.2 --retry 10 --retry-all-errors --retry-delay 5 \
    --max-time 120 -o "$AGENT_BINARY" "$AGENT_URL"
fi
check_sha256 "$AGENT_BINARY" "$(lock AGENT_SHA256)"
install -o root -g root -m 0755 "$AGENT_BINARY" /usr/local/bin/proxyhub-agent

echo "== users and directories"
id -u telemt >/dev/null 2>&1 || useradd --system --home-dir /var/lib/telemt --no-create-home \
  --shell /usr/sbin/nologin --user-group telemt
id -u xray >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home \
  --shell /usr/sbin/nologin --user-group xray
install -d -o root -g telemt -m 0750 /etc/telemt
install -d -o root -g xray -m 0750 /etc/xray
install -d -o telemt -g telemt -m 0700 /var/lib/telemt /var/lib/telemt/cache /var/lib/telemt/tlsfront
install -d -o root -g root -m 0700 /etc/proxyhub-agent /var/lib/proxyhub-agent

echo "== system configuration"
install -o root -g root -m 0644 "$FILES/telemt.service" /etc/systemd/system/telemt.service
install -o root -g root -m 0644 "$FILES/xray.service" /etc/systemd/system/xray.service
# The agent's Xray settings (paths, loopback API address, pinned version); no secret in it.
install -o root -g root -m 0600 "$FILES/agent-xray.json" /etc/proxyhub-agent/xray.json
install -o root -g root -m 0644 "$FILES/proxyhub-agent.service" /etc/systemd/system/proxyhub-agent.service
install -o root -g root -m 0644 "$FILES/proxyhub-lockdown.service" /etc/systemd/system/proxyhub-lockdown.service
install -o root -g root -m 0644 "$FILES/nftables.conf" /etc/nftables.conf
install -d -o root -g root -m 0755 /etc/systemd/journald.conf.d /etc/systemd/coredump.conf.d
install -o root -g root -m 0644 "$FILES/journald-proxyhub.conf" /etc/systemd/journald.conf.d/90-proxyhub.conf
printf '[Coredump]\nStorage=none\nProcessSizeMax=0\n' > /etc/systemd/coredump.conf.d/90-proxyhub.conf
chmod 0644 /etc/systemd/coredump.conf.d/90-proxyhub.conf
install -o root -g root -m 0644 "$FILES/sysctl-proxyhub.conf" /etc/sysctl.d/90-proxyhub.conf
printf 'tcp_bbr\n' > /etc/modules-load.d/proxyhub-bbr.conf
chmod 0644 /etc/modules-load.d/proxyhub-bbr.conf
rm -rf /var/log/journal  # a persistent journal directory would override Storage=volatile

nft -c -f /etc/nftables.conf
systemctl daemon-reload
systemctl enable nftables.service proxyhub-lockdown.service telemt.service xray.service \
  proxyhub-agent.service

if [ "$START" -eq 1 ]; then
  echo "== applying now"
  modprobe tcp_bbr
  sysctl --quiet --system
  systemctl restart systemd-journald.service
  systemctl restart nftables.service
  # --no-block: when run from cloud-init, these are ordered after cloud-final, which is still running.
  systemctl start --no-block proxyhub-lockdown.service proxyhub-agent.service
fi
echo "== done"

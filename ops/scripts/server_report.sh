#!/usr/bin/env bash
# Non-secret status report of the panel server (prints no passwords, tokens or keys).
set -uo pipefail
section() { printf '\n## %s\n' "$1"; }
section "system"
# shellcheck source=/dev/null
. /etc/os-release && echo "$PRETTY_NAME"; uname -r; nproc; free -h | head -2; df -h / | tail -1
section "time";     timedatectl show -p Timezone -p NTPSynchronized 2>/dev/null
section "users";    echo "current: $(id -un)"
if sudo -n true 2>/dev/null; then echo "sudo: ok (no password)"; else echo "sudo: needs password or not allowed"; fi
section "docker";   docker --version 2>/dev/null || echo "docker: not installed"; docker compose version 2>/dev/null || echo "compose: not installed"
section "listening ports"; ss -tlnH 2>/dev/null | awk '{print $4}' | sort -u
section "firewall (host)"; (sudo -n ufw status 2>/dev/null || echo "ufw: unavailable") | head -15
section "sshd (effective)"; sudo -n sshd -T 2>/dev/null | grep -Ei '^(port|passwordauthentication|permitrootlogin|pubkeyauthentication|kbdinteractiveauthentication) ' || echo "sshd -T: needs sudo"
section "updates";  systemctl is-enabled unattended-upgrades 2>/dev/null || echo "unattended-upgrades: not enabled"
section "proxy-hub"; ls -ld /etc/proxy-hub /opt/proxy-hub 2>/dev/null || echo "not installed yet"; systemctl is-active proxyhub-deployer.timer 2>/dev/null || true

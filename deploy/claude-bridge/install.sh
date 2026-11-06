#!/usr/bin/env bash
# Proxy-Hub Claude bridge installer (idempotent). Run as root on the panel server:
#   sudo bash deploy/claude-bridge/install.sh            first install (ops/prompts/P22.md)
#   sudo bash deploy/claude-bridge/install.sh --update   refresh the installed copy (the deployer)
#
# - group proxyhub-claude-sock (GID 10101): the socket's group; the worker container joins it;
# - user proxyhub-claude (system user, no shell, home /var/lib/proxyhub-claude, 0700): Claude Code
#   and the OWNER's subscription login live there and nowhere else (not in the panel, not here);
# - /var/lib/proxy-hub/claude-bridge (2750 proxyhub-claude:proxyhub-claude-sock): the socket's
#   directory, bind-mounted read-only into the worker;
# - the bridge itself as a reviewed copy in /usr/local/lib/proxyhub-claude-bridge (root-owned), and
#   its systemd unit.
# Claude Code is installed and logged in by P22, as that user; this script never touches a login.
set -euo pipefail

SOCK_GID=10101
SOCK_GROUP=proxyhub-claude-sock
USER_NAME=proxyhub-claude
HOME_DIR=/var/lib/proxyhub-claude
SOCK_DIR=/var/lib/proxy-hub/claude-bridge
LIB_DIR=/usr/local/lib/proxyhub-claude-bridge
UNIT=proxyhub-claude-bridge.service
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

# Copy $1 to $2 when it differs; prints "changed" when it did.
put() {
  if ! cmp -s "$1" "$2" 2>/dev/null; then
    install -m 0644 -o root -g root "$1" "$2"
    echo changed
  fi
}

if [ "${1:-}" = "--update" ]; then
  # Only an installed bridge is refreshed; a server without it is left alone.
  [ -f "/etc/systemd/system/$UNIT" ] || exit 0
  changed="$(put "$SRC/bridge.py" "$LIB_DIR/bridge.py")$(put "$SRC/$UNIT" "/etc/systemd/system/$UNIT")"
  if [ -n "$changed" ]; then
    systemctl daemon-reload
    systemctl try-restart "$UNIT"
    echo "claude bridge updated"
  fi
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is missing"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "python3 3.11 or later is needed"; exit 1; }

if getent group "$SOCK_GROUP" >/dev/null; then
  [ "$(getent group "$SOCK_GROUP" | cut -d: -f3)" = "$SOCK_GID" ] \
    || { echo "group $SOCK_GROUP exists with a GID other than $SOCK_GID"; exit 1; }
else
  if getent group "$SOCK_GID" >/dev/null; then
    echo "GID $SOCK_GID is taken by another group; the worker container joins GID $SOCK_GID"; exit 1
  fi
  groupadd --system --gid "$SOCK_GID" "$SOCK_GROUP"
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin \
    --user-group "$USER_NAME"
fi
usermod -a -G "$SOCK_GROUP" "$USER_NAME"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR" "$HOME_DIR/work" "$HOME_DIR/state"

# The socket's directory. Docker may have created it as root:root when the worker started first.
install -d -m 0755 -o root -g root /var/lib/proxy-hub
install -d -m 2750 -o "$USER_NAME" -g "$SOCK_GROUP" "$SOCK_DIR"
chown "$USER_NAME:$SOCK_GROUP" "$SOCK_DIR"
chmod 2750 "$SOCK_DIR"

install -d -m 0755 -o root -g root "$LIB_DIR"
put "$SRC/bridge.py" "$LIB_DIR/bridge.py" >/dev/null
put "$SRC/$UNIT" "/etc/systemd/system/$UNIT" >/dev/null
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"

echo "claude bridge installed: unit $UNIT, socket $SOCK_DIR/bridge.sock"
if [ ! -x "$HOME_DIR/.local/bin/claude" ]; then
  echo "next: install Claude Code for $USER_NAME and log in with the owner's subscription (P22)"
fi

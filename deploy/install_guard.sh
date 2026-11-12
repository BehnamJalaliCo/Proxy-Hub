#!/usr/bin/env bash
# The merge guard for deploy/install.sh (D31/D36): from main, the installer never checks out,
# installs or runs as root a commit that the deployer's guard would refuse.
#
#   install_guard.sh CHECKOUT TARGET
#
# - GitHub's PUBLIC signing key must be in /etc/proxy-hub/github-web-flow.gpg. It stays an operator
#   step (deploy/verify_main.sh): without it this prints the steps and exits 3.
# - The guard /usr/local/sbin/proxyhub-verify-main is installed from the verify_main.sh beside this
#   script only when there is none yet (first install). An installed guard is never replaced here:
#   only the deployer replaces it, after a good deploy.
# - Then the installed guard checks TARGET from the deployed commit (or TARGET alone, first install).
set -euo pipefail

ROOT="${PROXYHUB_DEPLOY_ROOT:-}"  # tests only, as in deploy/deployer.sh
ETC="$ROOT/etc/proxy-hub"
STATE="$ROOT/var/lib/proxy-hub"
guard="$ROOT/usr/local/sbin/proxyhub-verify-main"
key="$ETC/github-web-flow.gpg"
checkout="$1" target="$2"

if [ ! -s "$key" ]; then
  cat <<MSG

=== ACTION NEEDED (operator) ===
From main, the deployer runs only pull-request merges signed by GitHub (D31/D36). It needs GitHub's
PUBLIC commit-signing key in $key. Install it, then run this installer again:
  curl -fsS https://github.com/web-flow.gpg -o ~/web-flow.gpg
  gpg --show-keys --with-colons ~/web-flow.gpg | awk -F: '\$1=="fpr"{print \$10}'
    (the list must contain 968479A1AFF927E37D1A566BB5690EEEBB952194; if not, stop and report)
  sudo install -m 0644 -o root -g root ~/web-flow.gpg $key
MSG
  exit 3
fi

if [ ! -x "$guard" ]; then
  install -m 0755 "$(dirname "$0")/verify_main.sh" "$guard"
  echo "install-guard: installed the merge guard $guard (first install)"
fi

deployed="$(cat "$STATE/deployed_commit" 2>/dev/null || echo none)"
if ! "$guard" "$checkout" "$deployed" "$target" "$key"; then
  echo "install-guard: REFUSED: $target is not a chain of GitHub-signed pull-request merges; nothing was checked out, installed or deployed"
  exit 1
fi

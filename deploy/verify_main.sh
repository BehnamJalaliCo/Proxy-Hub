#!/usr/bin/env bash
# Deploy guard for the protected-main rule (decision D31/D36) without GitHub Pro.
#
#   verify_main.sh CHECKOUT BASE TARGET KEYRING
#
# Exit 0 only if EVERY first-parent commit in BASE..TARGET is a pull-request merge made by GitHub
# itself ("Merge pull request #N from ..." as GitHub titles it, or "Merge pull request #N: ..." when
# the merge was given a title, as PRs #19 and #20 were), with two parents, committed by GitHub and
# signed with GitHub's web-flow key whose fingerprint is pinned below. The title is only a sanity
# check; the parents, the committer and the signature are what count, and since D47 GitHub itself
# lets nothing onto main but a pull-request merge. A commit pushed straight to main breaks
# that chain (it is unsigned or single-parent), so it and everything after it are refused.
# BASE is the deployed commit ("none" on a first deploy); if it is not an ancestor of TARGET, the
# chain starts at their merge base. KEYRING is GitHub's public key (https://github.com/web-flow.gpg),
# installed by the operator in /etc/proxy-hub; its fingerprint must also equal the pinned one.
# Prints one line per commit and a final verdict; prints no secret.
set -euo pipefail

# GitHub web-flow commit signing key (GitHub docs, valid since 2024-01-16). Space separated; tests
# point PROXYHUB_GITHUB_MERGE_SIGNERS at their own key.
PINNED="${PROXYHUB_GITHUB_MERGE_SIGNERS:-968479A1AFF927E37D1A566BB5690EEEBB952194}"

checkout="$1" base="$2" target="$3" keyring="$4"
refuse() { echo "verify-main: REFUSED: $*"; exit 1; }

[ -s "$keyring" ] || refuse "GitHub signing key file $keyring is missing"
git -C "$checkout" cat-file -e "$target^{commit}" 2>/dev/null || refuse "unknown target commit"
target="$(git -C "$checkout" rev-parse "$target^{commit}")"

if [ "$base" = none ] || ! git -C "$checkout" cat-file -e "$base^{commit}" 2>/dev/null; then
  range=("$target" -1)  # first deploy: the tip itself must be a GitHub merge
elif git -C "$checkout" merge-base --is-ancestor "$base" "$target"; then
  range=("$base..$target")
else
  mb="$(git -C "$checkout" merge-base "$base" "$target")" || refuse "no common history with the deployed commit"
  range=("$mb..$target")
fi

GNUPGHOME="$(mktemp -d)"
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT
gpg --batch --quiet --import "$keyring" 2>/dev/null || refuse "cannot read the GitHub signing key file"
# github.com/web-flow.gpg holds every key GitHub has used (in 2026: the expired 2017 key first, then
# the current one), so the pinned key must be AMONG them. Each commit's signer is checked below on
# its own, and a signature by an expired key is refused (EXPKEYSIG).
imported="$(gpg --batch --with-colons --fingerprint 2>/dev/null | awk -F: '$1=="fpr"{print $10}')"
found=0
for p in $PINNED; do
  if grep -qx "$p" <<<"$imported"; then found=1; fi
done
[ "$found" = 1 ] || refuse "the key file does not contain the pinned GitHub key"

commits="$(git -C "$checkout" rev-list --first-parent "${range[@]}")"
count=0
for c in $commits; do
  count=$((count + 1))
  short="${c:0:12}"
  parents="$(git -C "$checkout" rev-list --parents -n 1 "$c" | wc -w)"
  [ "$parents" -eq 3 ] || refuse "$short is not a merge commit (a direct push to main?)"
  subject="$(git -C "$checkout" log -1 --format=%s "$c")"
  [[ "$subject" =~ ^Merge\ pull\ request\ \#[0-9]+(\ from\ |:\ ) ]] || refuse "$short is not a pull-request merge"
  committer="$(git -C "$checkout" log -1 --format='%cn <%ce>' "$c")"
  [ "$committer" = "GitHub <noreply@github.com>" ] || refuse "$short was not committed by GitHub"
  status="$(git -C "$checkout" verify-commit --raw "$c" 2>&1)" || refuse "$short has no valid GitHub signature"
  grep -qE '^\[GNUPG:\] (EXPKEYSIG|REVKEYSIG|BADSIG|ERRSIG)' <<<"$status" && refuse "$short: bad or expired signature"
  # VALIDSIG <sig fpr> ... <primary key fpr>: the primary key must be the pinned one.
  signer="$(awk '$2=="VALIDSIG"{print $NF}' <<<"$status")"
  case " $PINNED " in
    *" $signer "*) ;;
    *) refuse "$short is signed by ${signer:-nobody}, not by GitHub" ;;
  esac
  echo "verify-main: ok $short $subject"
done
echo "verify-main: OK ($count merge commit(s) checked up to ${target:0:12})"

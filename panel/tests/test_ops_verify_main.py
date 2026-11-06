"""deploy/verify_main.sh: from main, only GitHub-signed pull-request merges are deployed (D31/D36).

Real git and real GnuPG: a locally generated key stands in for GitHub's web-flow key (its
fingerprint is passed as PROXYHUB_GITHUB_MERGE_SIGNERS); a second key plays an impostor."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "verify_main.sh"
DEPLOYER = REPO / "deploy" / "deployer.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("git") is None, reason="needs git and gpg"
)


@dataclass
class Key:
    home: Path
    fpr: str
    public: Path


def _gpg(home: Path, *args: str, stdin: bytes | None = None) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv of test tools
        ["gpg", "--batch", "--homedir", str(home), *args],  # noqa: S607 - gpg from PATH
        input=stdin,
        capture_output=True,
        check=True,
    )
    return out.stdout.decode()


def _make_key(base: Path, name: str) -> Key:
    home = base / f"gnupg-{name}"
    home.mkdir(mode=0o700)
    _gpg(home, "--pinentry-mode", "loopback", "--passphrase", "", "--quick-gen-key",
         f"{name} <noreply@github.com>", "ed25519", "sign", "never")  # fmt: skip
    fpr = next(
        line.split(":")[9] for line in _gpg(home, "--with-colons", "--fingerprint").splitlines()
        if line.startswith("fpr:")
    )  # fmt: skip
    public = base / f"{name}.asc"
    public.write_text(_gpg(home, "--armor", "--export", fpr))
    return Key(home, fpr, public)


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Dev")
        self.git("config", "user.email", "dev@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "gpg.format", "openpgp")  # GitHub's web-flow signatures are OpenPGP
        self.commit("initial")

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        full = {**os.environ, **(env or {})}
        out = subprocess.run(  # noqa: S603 - fixed argv of test tools
            ["git", "-C", str(self.path), *args],  # noqa: S607 - git from PATH
            capture_output=True,
            check=True,
            env=full,
        )
        return out.stdout.decode().strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def commit(self, name: str) -> str:
        (self.path / f"{name}.txt").write_text(name)
        self.git("add", ".")
        self.git("commit", "-q", "-m", name)
        return self.head()

    def merge_pr(
        self,
        branch: str,
        number: int,
        *,
        key: Key | None,
        subject: str | None = None,
        committer: tuple[str, str] = ("GitHub", "noreply@github.com"),
    ) -> str:
        """Make a branch with one commit off main and merge it the way GitHub's button does."""
        base = self.head()
        self.git("checkout", "-q", "-b", branch)
        self.commit(f"{branch}-work")
        self.git("checkout", "-q", "main")
        message = subject or f"Merge pull request #{number} from owner/{branch}"
        args = ["merge", "--no-ff", "-q", "-m", message, branch]
        env = {"GIT_COMMITTER_NAME": committer[0], "GIT_COMMITTER_EMAIL": committer[1]}
        if key is not None:
            args.insert(1, f"-S{key.fpr}")
            env["GNUPGHOME"] = str(key.home)
        self.git(*args, env=env)
        assert self.git("rev-parse", "HEAD^1") == base
        return self.head()


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[Repo, Key, Key]]:
    github, impostor = _make_key(tmp_path, "github"), _make_key(tmp_path, "impostor")
    yield Repo(tmp_path / "repo"), github, impostor


def verify(
    repo: Repo, base: str, target: str, keyring: Path, signers: str
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PROXYHUB_GITHUB_MERGE_SIGNERS": signers}
    return subprocess.run(  # noqa: S603 - fixed argv: our own script
        ["bash", str(SCRIPT), str(repo.path), base, target, str(keyring)],  # noqa: S607 - bash from PATH
        capture_output=True,
        text=True,
        env=env,
    )


def test_pull_request_merges_signed_by_github_pass(world: tuple[Repo, Key, Key]) -> None:
    repo, github, _ = world
    start = repo.head()
    repo.merge_pr("a", 1, key=github)
    m2 = repo.merge_pr("b", 2, key=github)
    r = verify(repo, start, m2, github.public, github.fpr)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK (2 merge commit(s)" in r.stdout


def test_a_titled_pull_request_merge_passes(world: tuple[Repo, Key, Key]) -> None:
    """PRs #19 and #20 were merged with a title of their own ("Merge pull request #N: ...")."""
    repo, github, _ = world
    start = repo.head()
    m1 = repo.merge_pr("a", 19, key=github, subject="Merge pull request #19: HANDOFF.md, D46, D47")
    m2 = repo.merge_pr("b", 20, key=github)
    r = verify(repo, start, m2, github.public, github.fpr)
    assert r.returncode == 0 and f"ok {m1[:12]} Merge pull request #19: " in r.stdout, r.stdout


def test_same_commit_and_first_deploy(world: tuple[Repo, Key, Key]) -> None:
    repo, github, _ = world
    m1 = repo.merge_pr("a", 1, key=github)
    assert verify(repo, m1, m1, github.public, github.fpr).returncode == 0  # config-only redeploy
    assert verify(repo, "none", m1, github.public, github.fpr).returncode == 0  # tip checked alone


def test_a_direct_push_to_main_is_refused_even_under_a_later_good_merge(world: tuple[Repo, Key, Key]) -> None:
    repo, github, _ = world
    m1 = repo.merge_pr("a", 1, key=github)
    pushed = repo.commit("pushed-straight-to-main")
    r = verify(repo, m1, pushed, github.public, github.fpr)
    assert r.returncode == 1 and "not a merge commit" in r.stdout
    m2 = repo.merge_pr("b", 2, key=github)  # a good merge on top does not launder the push
    r = verify(repo, m1, m2, github.public, github.fpr)
    assert r.returncode == 1 and pushed[:12] in r.stdout


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"key": None}, "no valid GitHub signature"),
        ({"subject": "Merge branch 'a'"}, "not a pull-request merge"),
        ({"subject": "Merge pull request #1"}, "not a pull-request merge"),
        ({"subject": "Merge pull request #x: title"}, "not a pull-request merge"),
        ({"subject": "Merged pull request #1: title"}, "not a pull-request merge"),
        ({"committer": ("Mallory", "m@example.invalid")}, "not committed by GitHub"),
    ],
)
def test_merges_not_made_by_github_are_refused(
    world: tuple[Repo, Key, Key], kwargs: dict[str, object], reason: str
) -> None:
    repo, github, _ = world
    start = repo.head()
    options: dict[str, object] = {"key": github, **kwargs}
    target = repo.merge_pr("a", 1, **options)  # type: ignore[arg-type]
    r = verify(repo, start, target, github.public, github.fpr)
    assert r.returncode == 1 and reason in r.stdout, r.stdout


def test_a_merge_signed_by_another_key_is_refused(world: tuple[Repo, Key, Key]) -> None:
    repo, github, impostor = world
    start = repo.head()
    target = repo.merge_pr("a", 1, key=impostor)
    # The impostor's key in the keyring does not help: its fingerprint is not pinned.
    r = verify(repo, start, target, impostor.public, github.fpr)
    assert r.returncode == 1 and "does not contain the pinned GitHub key" in r.stdout
    # With GitHub's key in the keyring the impostor's signature cannot be verified at all.
    r = verify(repo, start, target, github.public, github.fpr)
    assert r.returncode == 1 and "no valid GitHub signature" in r.stdout


def test_the_signer_of_each_commit_must_be_the_pinned_key(
    world: tuple[Repo, Key, Key], tmp_path: Path
) -> None:
    """A key file holding GitHub's key AND another one: a commit signed by the other is refused."""
    repo, github, impostor = world
    both = tmp_path / "both.asc"
    both.write_text(github.public.read_text() + impostor.public.read_text())
    start = repo.head()
    target = repo.merge_pr("a", 1, key=impostor)
    r = verify(repo, start, target, both, github.fpr)
    assert r.returncode == 1 and f"signed by {impostor.fpr}" in r.stdout, r.stdout


def test_the_pinned_key_may_come_after_other_keys_in_the_file(
    world: tuple[Repo, Key, Key], tmp_path: Path
) -> None:
    """Found on the panel server (P08): github.com/web-flow.gpg lists GitHub's expired 2017 key
    FIRST and the current key second. The guard must find the pinned key anywhere in the file."""
    repo, github, old = world
    both = tmp_path / "web-flow.asc"
    both.write_text(old.public.read_text() + github.public.read_text())  # another key first
    start = repo.head()
    target = repo.merge_pr("a", 1, key=github)
    r = verify(repo, start, target, both, github.fpr)
    assert r.returncode == 0, r.stdout
    assert "OK (1 merge commit(s)" in r.stdout


def test_missing_keyring_or_unknown_target_is_refused(world: tuple[Repo, Key, Key], tmp_path: Path) -> None:
    repo, github, _ = world
    start = repo.head()
    target = repo.merge_pr("a", 1, key=github)
    r = verify(repo, start, target, tmp_path / "absent.gpg", github.fpr)
    assert r.returncode == 1 and "missing" in r.stdout
    r = verify(repo, start, "0" * 40, github.public, github.fpr)
    assert r.returncode == 1 and "unknown target" in r.stdout


def test_a_base_off_main_starts_the_chain_at_the_merge_base(world: tuple[Repo, Key, Key]) -> None:
    """Switching from the work branch: the deployed commit is not on main's first-parent line."""
    repo, github, _ = world
    repo.git("checkout", "-q", "-b", "work")
    deployed = repo.commit("work-tip")
    repo.git("checkout", "-q", "main")
    target = repo.merge_pr("c", 3, key=github)
    r = verify(repo, deployed, target, github.public, github.fpr)
    assert r.returncode == 0, r.stdout


def test_the_deployer_runs_the_installed_guard_before_deploying_from_main() -> None:
    text = DEPLOYER.read_text()
    guard_at = text.index('"$guard" "$CHECKOUT" "$current" "$target" "$ETC/github-web-flow.gpg"')
    assert guard_at < text.index('log "deploying $target')
    assert '[ "$DEPLOY_BRANCH" = main ]' in text
    assert "guard=/usr/local/sbin/proxyhub-verify-main" in text  # the trusted copy, not the target's
    assert '[ -x "$guard" ] &&' in text  # a missing guard refuses (fail closed)
    assert 'install -m 0755 "$CHECKOUT/deploy/verify_main.sh" /usr/local/sbin/proxyhub-verify-main' in text

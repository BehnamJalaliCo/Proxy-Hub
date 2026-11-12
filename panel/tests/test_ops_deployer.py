"""deploy/deployer.sh run for real in a sandbox (PROXYHUB_DEPLOY_ROOT).

Real bash, git and flock; a local git origin stands in for GitHub. Fake ``docker``, ``curl`` and
``sleep`` on PATH log every call: ``docker compose up`` records the commit it "starts", and ``curl``
answers /healthz for that commit the way the fake's control files say. What is proven: a failed
deploy is recorded with its deploy.env and raised as a metric, is not retried except once after a
deploy.env edit, rolls back without running migrate, and a good deploy clears it all; the health
gate wants a worker heartbeat written after ``compose up``; old panel images are removed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
DEPLOYER = DEPLOY / "deployer.sh"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "git", "flock", "sha256sum", "sleep")),
    reason="needs bash, git, flock, sha256sum and sleep",
)

FAKE_DOCKER = """#!/usr/bin/env bash
# Fake docker: logs each call with the commit being deployed; "compose up" marks that commit running.
d="$FAKE_DIR"
echo "docker $* [commit=${PROXYHUB_GIT_COMMIT:-}]" >> "$d/calls"
case " $* " in
  *" compose "*" build "*) [ -e "$d/fail_build_$PROXYHUB_GIT_COMMIT" ] && exit 1; exit 0 ;;
  *" compose "*" up "*) echo "$PROXYHUB_GIT_COMMIT" > "$d/running"; exit 0 ;;
  *" image ls "*) cat "$d/tags" 2>/dev/null; exit 0 ;;
  *" image rm "*) [ -e "$d/image_rm_fails" ] && exit 1; exit 0 ;;
esac
exit 0
"""

# /healthz of the "running" commit: 503 when marked unhealthy, else 200 with the heartbeat age in
# the file "age" (default 0.4 s: written after "compose up"), or with no age at all ("no_age").
FAKE_CURL = """#!/usr/bin/env bash
d="$FAKE_DIR"
echo "curl $*" >> "$d/calls"
running="$(cat "$d/running" 2>/dev/null || echo none)"
[ -e "$d/unhealthy_$running" ] && exit 22
if [ -e "$d/no_age" ]; then
  printf '{"version":"0","commit":"%s","db":true,"redis":true,"status":"ok"}' "$running"
else
  printf '{"version":"0","commit":"%s","db":true,"worker_heartbeat_age_s":%s,"redis":true,"status":"ok"}' \\
    "$running" "$(cat "$d/age" 2>/dev/null || echo 0.4)"
fi
"""


@dataclass
class Sandbox:
    root: Path
    origin: Path
    fake: Path
    env: dict[str, str]

    @property
    def state(self) -> Path:
        return self.root / "var/lib/proxy-hub"

    @property
    def deploy_env(self) -> Path:
        return self.root / "etc/proxy-hub/deploy.env"

    def git(self, *args: str) -> str:
        out = subprocess.run(  # noqa: S603 - fixed argv of test tools
            ["git", "-C", str(self.origin), *args],  # noqa: S607 - git from PATH
            capture_output=True,
            check=True,
            text=True,
            env=self.env,
        )
        return out.stdout.strip()

    def commit(self, name: str) -> str:
        (self.origin / f"{name}.txt").write_text(name)
        self.git("add", ".")
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv: our own script
            ["bash", str(DEPLOYER)],  # noqa: S607 - bash from PATH
            capture_output=True,
            text=True,
            env=self.env,
            timeout=120,
        )

    def calls(self) -> list[str]:
        path = self.fake / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def read(self, name: str) -> str | None:
        path = self.state / name
        return path.read_text().strip() if path.exists() else None

    def metric(self) -> tuple[str, str, str]:
        """(refused, failed, rollback_failed) from the node-exporter textfile."""
        text = (self.state / "metrics" / "proxyhub_deploy.prom").read_text()
        values = dict(line.split(" ", 1) for line in text.splitlines() if not line.startswith("#"))
        return (
            values["proxyhub_deploy_refused"],
            values["proxyhub_deploy_failed"],
            values["proxyhub_deploy_rollback_failed"],
        )

    def mark(self, name: str, content: str = "") -> None:
        (self.fake / name).write_text(content)

    def edit_deploy_env(self) -> None:
        self.deploy_env.write_text(self.deploy_env.read_text() + "# edited\n")

    def cfg_hash(self) -> str:
        return hashlib.sha256(self.deploy_env.read_bytes()).hexdigest()


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _builds(calls: list[str]) -> list[str]:
    """The commit of each 'compose build' call, in order."""
    return [c.rsplit("[commit=", 1)[1].rstrip("]") for c in calls if " compose " in c and " build " in c]


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    root, origin, fake, bin_dir = (tmp_path / n for n in ("root", "origin", "fake", "bin"))
    for d in ("etc/proxy-hub", "var/lib/proxy-hub/metrics", "usr/local/sbin", "run"):
        (root / d).mkdir(parents=True)
    for d in (origin / "deploy", fake, bin_dir):
        d.mkdir(parents=True)
    _write_exec(bin_dir / "docker", FAKE_DOCKER)
    _write_exec(bin_dir / "curl", FAKE_CURL)
    _write_exec(bin_dir / "sleep", f"#!/bin/sh\nexec {shutil.which('sleep')} 0.2\n")
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Dev\n\temail = dev@example.invalid\n[commit]\n\tgpgsign = false\n")
    env = {k: v for k, v in os.environ.items() if k not in {"FORCE", "PROXYHUB_METRICS_DIR"}}
    env |= {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROXYHUB_DEPLOY_ROOT": str(root),
        "PROXYHUB_DEPLOY_HEALTH_DEADLINE_S": "4",
        "FAKE_DIR": str(fake),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    box = Sandbox(root, origin, fake, env)
    box.git("init", "-q", "-b", "work")
    for name in ("deployer.sh", "verify_main.sh", "known_hosts.github", "docker-compose.yml"):
        shutil.copy(DEPLOY / name, origin / "deploy" / name)
    box.commit("base")
    box.git("clone", "-q", str(origin), str(root / "opt/proxy-hub"))
    box.deploy_env.write_text("DEPLOY_BRANCH=work\nPANEL_DOMAIN=\n")
    return box


def test_a_failed_deploy_is_raised_not_retried_and_cleared_by_a_good_deploy(box: Sandbox) -> None:
    good = box.commit("good")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.read("deployed_commit") == good and f"ok: {good} is healthy" in r.stdout
    assert box.metric() == ("0", "0", "0")

    bad = box.commit("bad")
    box.mark(f"unhealthy_{bad}")
    before = len(box.calls())
    r = box.run()
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAILED" in r.stdout and "rollback ok" in r.stdout
    assert box.read("failed_commit") == bad and box.read("failed_config_hash") == box.cfg_hash()
    assert box.read("deployed_commit") == good
    assert box.metric() == ("0", "1", "0")
    ups = [c for c in box.calls()[before:] if " compose " in c and " up " in c]
    assert len(ups) == 2
    assert f"[commit={bad}]" in ups[0] and "--scale" not in ups[0]  # a deploy runs migrate
    # The rollback runs no migrate: the bad commit may have migrated the database past the old code.
    assert f"[commit={good}]" in ups[1] and "--scale migrate=0" in ups[1]

    # Known bad, same deploy.env: nothing at all happens on the next ticks.
    before = len(box.calls())
    assert box.run().returncode == 0 and box.run().returncode == 0
    assert box.calls()[before:] == []

    # deploy.env edited: exactly one new try of the bad commit (it fails at build this time) ...
    (box.fake / f"unhealthy_{bad}").unlink()
    box.mark(f"fail_build_{bad}")
    box.edit_deploy_env()
    before = len(box.calls())
    r = box.run()
    assert r.returncode == 1 and "compose build failed" in r.stdout
    assert _builds(box.calls()[before:]) == [bad, good]
    assert box.read("failed_config_hash") == box.cfg_hash()
    # ... and then it is skipped again, not redeployed on every timer tick.
    before = len(box.calls())
    assert box.run().returncode == 0
    assert box.calls()[before:] == []

    fixed = box.commit("fixed")
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.read("deployed_commit") == fixed
    assert box.read("failed_commit") is None and box.read("failed_config_hash") is None
    assert box.metric() == ("0", "0", "0")


def test_state_of_the_older_deployer_keeps_skipping_a_known_bad_commit(box: Sandbox) -> None:
    """No failed_config_hash yet (written by the deployer before this one): same as before."""
    good = box.commit("good")
    assert box.run().returncode == 0
    bad = box.commit("bad")
    (box.state / "failed_commit").write_text(bad + "\n")
    before = len(box.calls())
    assert box.run().returncode == 0
    assert box.calls()[before:] == []
    assert box.read("deployed_commit") == good


def test_a_failed_rollback_is_raised(box: Sandbox) -> None:
    good = box.commit("good")
    assert box.run().returncode == 0
    bad = box.commit("bad")
    box.mark(f"fail_build_{bad}")
    box.mark(f"fail_build_{good}")  # e.g. the registry is down for the rollback build too
    r = box.run()
    assert r.returncode == 1 and "ROLLBACK FAILED" in r.stdout
    assert box.metric() == ("0", "1", "1")


def test_a_failing_first_deploy_has_nothing_to_roll_back(box: Sandbox) -> None:
    first = box.commit("first")
    box.mark(f"fail_build_{first}")
    r = box.run()
    assert r.returncode == 1 and "rolling back" not in r.stdout
    assert box.read("failed_commit") == first
    assert box.metric() == ("0", "1", "0")


def test_a_config_only_redeploy_that_passes_on_the_second_try_is_a_good_deploy(box: Sandbox) -> None:
    good = box.commit("good")
    assert box.run().returncode == 0
    box.edit_deploy_env()
    box.mark("age", "30")  # the first try sees only an old heartbeat
    with subprocess.Popen(  # noqa: S603 - fixed argv: our own script
        ["bash", str(DEPLOYER)],  # noqa: S607 - bash from PATH
        env=box.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        assert proc.stdout is not None
        lines: list[str] = []
        for line in proc.stdout:
            lines.append(line)
            if line.startswith("proxyhub-deploy: FAILED"):
                box.mark("age", "0.4")  # the worker beats again before the second try
        code = proc.wait(timeout=120)
    out = "".join(lines)
    assert code == 0, out
    assert f"ok: {good} is healthy on the second try" in out
    assert box.read("failed_commit") is None and box.read("deployed_config_hash") == box.cfg_hash()
    assert box.metric() == ("0", "0", "0")


@pytest.mark.parametrize(
    ("control", "content"),
    [("age", "30"), ("no_age", "")],
    ids=["heartbeat-from-before-the-deploy", "no-heartbeat-age"],
)
def test_the_health_gate_wants_a_heartbeat_written_after_compose_up(
    box: Sandbox, control: str, content: str
) -> None:
    """A 200 from /healthz on the OLD worker's heartbeat (fresh for 90 s) proves nothing about the
    worker just started: such a deploy fails."""
    commit = box.commit("c")
    box.mark(control, content)
    r = box.run()
    assert r.returncode == 1, r.stdout
    assert "proxyhub-deploy: health: " in r.stdout
    assert box.read("deployed_commit") is None and box.read("failed_commit") == commit


def test_the_gate_clock_starts_when_compose_up_returns() -> None:
    text = DEPLOYER.read_text()
    up = text.index("compose up -d --remove-orphans || ")
    mark = text.index("local up_done=$SECONDS")
    gate = text.index('wait_healthy "$up_done"')
    assert up < mark < gate


def test_old_panel_images_are_removed_but_never_the_running_one_or_the_rollback_target(box: Sandbox) -> None:
    previous = box.commit("previous")
    assert box.run().returncode == 0
    new = box.commit("new")
    old = "a" * 40
    box.mark("tags", f"{old}\n{previous}\n{new}\ndev\n<none>\n")
    before = len(box.calls())
    r = box.run()
    assert r.returncode == 0, r.stdout
    removed = [c.split()[3] for c in box.calls()[before:] if c.startswith("docker image rm ")]
    assert removed == [f"proxyhub/panel:{old}", "proxyhub/panel:dev"]

    # A refused removal (the image is still in use) never fails the deploy.
    newer = box.commit("newer")
    box.mark("tags", f"{previous}\n{new}\n{newer}\n")
    box.mark("image_rm_fails")
    r = box.run()
    assert r.returncode == 0 and f"ok: {newer} is healthy" in r.stdout
    assert f"kept image proxyhub/panel:{previous[:12]}" in r.stdout


def test_from_main_a_missing_guard_refuses_and_names_the_cause(box: Sandbox) -> None:
    box.git("branch", "-m", "work", "main")
    box.deploy_env.write_text("DEPLOY_BRANCH=main\nPANEL_DOMAIN=\n")
    target = box.git("rev-parse", "HEAD")
    r = box.run()
    assert r.returncode == 1
    assert "the merge guard" in r.stdout and "is not installed" in r.stdout
    assert "not a GitHub-signed" not in r.stdout
    assert box.calls() == []
    assert box.read("failed_commit") == target
    assert box.metric() == ("1", "0", "0")


def test_a_malformed_panel_domain_fails_the_deploy_before_anything_is_built(box: Sandbox) -> None:
    box.commit("c")
    box.deploy_env.write_text("DEPLOY_BRANCH=work\nPANEL_DOMAIN='panel.example.com/x'\n")
    r = box.run()
    assert r.returncode == 1 and "PANEL_DOMAIN must be a lower-case DNS name" in r.stdout
    assert _builds(box.calls()) == []

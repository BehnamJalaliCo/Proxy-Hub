"""Monitoring and backup files (M1.7) checked against the code and the project rules.

Runtime behaviour is proven elsewhere for real (deploy/monitoring/check_configs.sh runs promtool,
amtool, loki and alloy on these files; verify_stack.sh checks a running stack). These tests keep
the pieces consistent with each other on every CI run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
RULES = sorted((DEPLOY / "monitoring" / "prometheus" / "rules").glob("*.yml"))
MONITORING_SERVICES = {"prometheus", "alertmanager", "loki", "alloy", "grafana", "node-exporter"}
PINNED = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")


def _compose() -> dict[str, Any]:
    data = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _default(ref: str) -> str:
    """'${VAR:-image@sha256:...}' -> 'image@sha256:...'; plain references unchanged."""
    m = re.fullmatch(r"\$\{[A-Z0-9_]+:-(.+)\}", ref)
    return m.group(1) if m else ref


def _rules_text() -> str:
    assert RULES, "no alert rule files"
    return "\n".join(p.read_text(encoding="utf-8") for p in RULES)


def test_worker_stale_rule_matches_code() -> None:
    """The Prometheus alert and /healthz use the same staleness threshold."""
    from proxyhub.api.app import WORKER_STALE_SECONDS

    found = re.findall(r"proxyhub_worker_heartbeat_age_seconds\s*>\s*(\d+)", _rules_text())
    assert found and {int(x) for x in found} == {WORKER_STALE_SECONDS}


def test_every_series_an_alert_reads_is_produced() -> None:
    """Every proxyhub_* series an alert reads is exported by the panel or written by a backup script."""
    import proxyhub.api.app as app_module

    used = set(re.findall(r"\b(proxyhub_[a-z_]+)\b", _rules_text()))
    exported = set(re.findall(r'"(proxyhub_[a-z_]+)"', Path(app_module.__file__).read_text(encoding="utf-8")))
    scripts = "\n".join(p.read_text(encoding="utf-8") for p in sorted((DEPLOY / "backup").glob("*.sh")))
    written = set(re.findall(r"^(proxyhub_[a-z_]+) ", scripts, re.MULTILINE))
    missing = sorted(used - exported - written)
    assert used and not missing, f"alert rules read series nobody produces: {missing}"


def test_required_alerts_exist() -> None:
    """The M1.7 alert set: api down, worker stale, disk, backup age, restore test failing."""
    names = set(re.findall(r"^\s*- alert: (\w+)", _rules_text(), re.MULTILINE))
    assert {
        "PanelApiDown",
        "WorkerHeartbeatStale",
        "DiskAlmostFull",
        "BackupTooOld",
        "RestoreTestFailing",
    } <= names
    assert "26 * 3600" in _rules_text() and "> 85" in _rules_text()


def test_every_external_image_is_pinned_by_digest() -> None:
    refs: list[str] = []
    for name, svc in _compose()["services"].items():
        image = svc.get("image", "")
        if image and not image.startswith("proxyhub/"):
            refs.append(f"{name}: {_default(image)}")
        base = svc.get("build", {}).get("args", {}).get("BASE_IMAGE")
        if base:
            refs.append(f"{name} (base): {_default(base)}")
    assert len(refs) >= 9, refs
    unpinned = [r for r in refs if not PINNED.match(r.split(": ", 1)[1])]
    assert not unpinned, f"not pinned by digest: {unpinned}"


def test_monitoring_is_not_public() -> None:
    """Only Grafana is published, and only on loopback. Nothing listens on a public interface."""
    services = _compose()["services"]
    assert MONITORING_SERVICES <= set(services)
    published = {n: s.get("ports", []) for n, s in services.items() if n in MONITORING_SERVICES}
    assert published.pop("grafana") == ["127.0.0.1:3000:3000"]
    assert all(not ports for ports in published.values()), published
    for name in MONITORING_SERVICES:
        assert services[name].get("network_mode") != "host", name


def test_only_panel_containers_are_labelled_for_log_shipping() -> None:
    """Alloy ships only lines carrying io.proxyhub.logs=panel (rule 7: no node or user logs)."""
    services = _compose()["services"]
    labelled = {n for n, s in services.items() if (s.get("labels") or {}).get("io.proxyhub.logs") == "panel"}
    assert labelled == {"migrate", "api", "worker"}
    for name in labelled:
        opts = services[name]["logging"]["options"]
        assert "io.proxyhub.logs" in opts["labels"].split(","), name
    alloy = (DEPLOY / "monitoring" / "alloy" / "config.alloy").read_text(encoding="utf-8")
    assert 'selector            = "{ship!=\\"panel\\"}"' in alloy and 'action              = "drop"' in alloy
    assert alloy.count("stage.replace") >= 2, "IPv4 and IPv6 redaction stages"
    # The shipper never gets the Docker socket (root-equivalent).
    assert not any("docker.sock" in str(v) for v in services["alloy"].get("volumes", []))


def test_new_secrets_are_files_created_by_ensure_secrets() -> None:
    """Rule 3: every compose secret is a file that ensure_secrets.sh creates; none comes from env."""
    compose = _compose()
    ensure = (DEPLOY / "ensure_secrets.sh").read_text(encoding="utf-8")
    for name, spec in compose["secrets"].items():
        assert spec == {"file": f"/etc/proxy-hub/secrets/{name}"}, name
        assert re.search(rf"^(gen_hex|gen_ec_key|optional) {name}\b", ensure, re.MULTILINE), name
    assert re.search(r"^gen_age_key backup_age_key\b", ensure, re.MULTILINE)
    grafana_env = compose["services"]["grafana"]["environment"]
    assert grafana_env["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/secrets/grafana_admin_password"
    assert "GF_SECURITY_ADMIN_PASSWORD" not in grafana_env


def test_backup_units_are_installed() -> None:
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    for unit in ("proxyhub-backup", "proxyhub-restore-test"):
        assert (DEPLOY / "systemd" / f"{unit}.service").exists()
        assert (DEPLOY / "systemd" / f"{unit}.timer").exists()
        assert f"{unit}.timer" in install
    assert "apt-get install -y -qq --no-install-recommends age" in install


ALERTMANAGER = DEPLOY / "monitoring" / "alertmanager"


def _redact(text: str, secret_files: list[Path]) -> str:
    """Run the real redact.awk (host awk) over text, the way entrypoint.sh and safe_logs.sh do."""
    import shutil
    import subprocess

    awk = shutil.which("awk")
    assert awk, "awk is needed for the log redactor"
    # Fixed argv, no shell: the resolved awk binary and a repo file; only test data on stdin.
    done = subprocess.run(  # noqa: S603
        [
            awk,
            "-v",
            "secret_files=" + " ".join(str(p) for p in secret_files),
            "-f",
            str(ALERTMANAGER / "redact.awk"),
        ],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return done.stdout


def test_log_redactor_removes_bot_token_and_secret_urls(tmp_path: Path) -> None:
    """Rule 3: the exact line Alertmanager v0.34.2 logs on a failed Telegram send loses the token."""
    import secrets

    token = f"{secrets.randbelow(10**9) + 10**8}:{secrets.token_urlsafe(26)}"
    half = token.split(":", 1)[1]
    url = f"https://hc-ping.com/{secrets.token_hex(16)}"
    (tmp_path / "alert_bot_token").write_text(token + "\n", encoding="utf-8")
    (tmp_path / "healthchecks_watchdog_url").write_text(url + "\r\n", encoding="utf-8")
    other = f"{secrets.randbelow(10**9) + 10**8}:{secrets.token_urlsafe(26)}"  # not in any file
    lines = [
        'level=WARN msg="Notify attempt failed, will retry later" integration=telegram[0] '
        f'err="telebot: Post \\"https://api.telegram.org/bot{token}/sendMessage\\": dial tcp: timeout"',
        f"half alone {half} and url {url} and twice {token}{token}",
        f"other token in a Bot API URL /bot{other}/getMe and encoded /bot{other.replace(':', '%3A')}/x",
        'level=INFO msg="Listening on" address=0.0.0.0:9093 ts=2026-11-03T10:11:12Z',
    ]
    out = _redact(
        "\n".join(lines) + "\n", [tmp_path / "alert_bot_token", tmp_path / "healthchecks_watchdog_url"]
    )
    out_lines = out.splitlines()
    assert len(out_lines) == len(lines)
    for secret in (token, half, url, other, other.split(":", 1)[1]):
        assert secret not in out
    assert "/bot<redacted>/sendMessage" in out_lines[0] and "dial tcp: timeout" in out_lines[0]
    assert out_lines[1] == "half alone <redacted> and url <redacted> and twice <redacted><redacted>"
    assert out_lines[2] == "other token in a Bot API URL /bot<redacted>/getMe and encoded /bot<redacted>/x"
    assert out_lines[3] == lines[3], "lines without a secret pass unchanged"


def test_log_redactor_ignores_missing_empty_and_short_files(tmp_path: Path) -> None:
    """The receivers are off by default (empty files): the filter must pass logs through untouched."""
    (tmp_path / "empty").write_text("", encoding="utf-8")
    (tmp_path / "short").write_text("-100\n", encoding="utf-8")  # < 8 chars: would mangle normal text
    text = "a -100 b\nc 9093 d\n"
    assert _redact(text, [tmp_path / "empty", tmp_path / "short", tmp_path / "missing"]) == text


def test_alertmanager_runs_behind_the_redactor() -> None:
    """Alertmanager is never exec'd directly, its output goes to the redactor, secrets never in argv."""
    entry = (ALERTMANAGER / "entrypoint.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in entry.splitlines() if not line.lstrip().startswith("#"))
    assert "exec /bin/alertmanager" not in code
    assert re.search(r'--log\.level=info >"\$LOG_FIFO" 2>&1 &', code)
    assert re.search(r'-f /etc/alertmanager/proxyhub/redact\.awk <"\$LOG_FIFO"', code)
    assert 'secret_files="$RUNDIR/secrets/alert_bot_token $RUNDIR/secrets/healthchecks_watchdog_url"' in code
    assert 'trap \'kill -TERM "$am"' in code, "SIGTERM must be forwarded to Alertmanager"
    assert "redact.awk" in (ALERTMANAGER / "Dockerfile").read_text(encoding="utf-8")
    safe_logs = (DEPLOY / "monitoring" / "safe_logs.sh").read_text(encoding="utf-8")
    assert "alertmanager/redact.awk" in safe_logs and "cat " not in safe_logs

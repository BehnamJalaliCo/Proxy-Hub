"""Monitoring and backup files (M1.7) checked against the code and the project rules.

Runtime behaviour is proven elsewhere for real (deploy/monitoring/check_configs.sh runs promtool,
amtool, loki and alloy on these files; verify_stack.sh checks a running stack). These tests keep
the pieces consistent with each other on every CI run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
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
    """Every proxyhub_* series an alert reads is exported by the panel or written by a host script
    (backup and restore test, and the deployer's merge guard)."""
    import proxyhub.api.app as app_module

    used = set(re.findall(r"\b(proxyhub_[a-z_]+)\b", _rules_text()))
    exported = set(re.findall(r'"(proxyhub_[a-z_]+)"', Path(app_module.__file__).read_text(encoding="utf-8")))
    host_scripts = [*sorted((DEPLOY / "backup").glob("*.sh")), DEPLOY / "deployer.sh"]
    scripts = "\n".join(p.read_text(encoding="utf-8") for p in host_scripts)
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
        assert "pid" not in services[name], f"{name}: no host PID namespace"


def test_node_exporter_reads_the_host_procfs_without_host_pid() -> None:
    """No host PID namespace; the host's /proc and /sys come through the read-only bind of / instead.

    With the container's own /proc, the filesystem collector would read the container's mount table
    (found for real: /etc/hosts and friends showed up as disks and fired DiskAlmostFull twice).
    """
    svc = _compose()["services"]["node-exporter"]
    assert "pid" not in svc
    assert svc["volumes"] == ["/:/host:ro,rslave"]
    assert {"--path.rootfs=/host", "--path.procfs=/host/proc", "--path.sysfs=/host/sys"} <= set(
        svc["command"]
    )


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


def test_every_container_log_rotates() -> None:
    """The json-file default never rotates and can fill the panel disk. Rotation must not add the
    io.proxyhub.logs label to anything but the panel containers (checked above)."""
    for name, svc in _compose()["services"].items():
        logging = svc.get("logging") or {}
        assert logging.get("driver") == "json-file", name
        assert logging.get("options", {}).get("max-size") and logging["options"].get("max-file"), name


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


def test_the_telegram_fallback_shows_no_stale_description_for_a_resolved_alert() -> None:
    """A resolved alert keeps its last firing annotations ("... the control loops do not run"):
    only the firing branch may print the description (check_configs.sh renders it with amtool)."""
    tmpl = (ALERTMANAGER / "telegram.tmpl").read_text(encoding="utf-8")
    body = tmpl[tmpl.index("{{ range .Alerts -}}") :]
    assert body.count(".Annotations.description") == 1
    firing = body.index('{{ if eq .Status "firing" }}')
    description = body.index(".Annotations.description")
    assert firing < description < body.index("{{ else }}", firing)
    assert body.count(".Annotations.summary") == 2  # both branches name the alert


def _render_alertmanager(tmp_path: Path, token: str, chat_id: str, watchdog: str) -> tuple[int, str, Path]:
    """Run the real entrypoint.sh with the host's POSIX sh, paths moved into tmp_path and amtool
    left out (PROXYHUB_RENDER_ONLY=1): (exit code, output, the tmpfs secrets directory)."""
    import shutil
    import subprocess

    sh = shutil.which("sh")
    assert sh, "a POSIX sh is needed"
    secrets, rundir = tmp_path / "secrets", tmp_path / "rundir"
    secrets.mkdir(parents=True)
    for name, value in (
        ("alert_bot_token", token),
        ("alert_chat_id", chat_id),
        ("healthchecks_watchdog_url", watchdog),
    ):
        (secrets / name).write_text(value + "\n" if value else "", encoding="utf-8")
    text = (ALERTMANAGER / "entrypoint.sh").read_text(encoding="utf-8")
    for old, new in (
        ("SECRETS=/run/secrets", f"SECRETS={secrets}"),
        ("TPL=/etc/alertmanager/proxyhub/telegram.tmpl", f"TPL={ALERTMANAGER / 'telegram.tmpl'}"),
        ("RUNDIR=/tmp/alertmanager", f"RUNDIR={rundir}"),
        ('exec /bin/amtool check-config "$CONFIG"', "exit 0"),
    ):
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    script = tmp_path / "entrypoint.sh"
    script.write_text(text, encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - fixed argv: host sh and a copy of our own script
        [sh, str(script)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PROXYHUB_RENDER_ONLY": "1"},
        timeout=30,
    )
    return done.returncode, done.stdout + done.stderr, rundir / "secrets"


@pytest.mark.parametrize("junk", ["0", "-", "no", "x", "true"])
def test_short_junk_in_an_optional_secret_only_turns_that_receiver_off(tmp_path: Path, junk: str) -> None:
    """'0' or 'no' is also text of the rendered config: the leak check refused to start Alertmanager
    (and with it the panel webhook) although such a value is never used."""
    code, out, copies = _render_alertmanager(tmp_path, junk, "-1001234567890", junk)
    assert code == 0, out
    assert "panel webhook: on; telegram receiver: off; external watchdog: off" in out
    assert sorted(p.name for p in copies.iterdir()) == []  # copies of receivers that are off: removed


def test_a_valid_telegram_receiver_is_still_on_and_checked(tmp_path: Path) -> None:
    token = "123456:not-a-real-token-config-check-only"
    code, out, copies = _render_alertmanager(tmp_path, token, "-1001234567890", "")
    assert code == 0, out
    assert "telegram receiver: on" in out
    assert (copies / "alert_bot_token").read_text(encoding="utf-8") == token  # read by bot_token_file


def test_a_chat_id_beyond_telegram_s_range_turns_telegram_off(tmp_path: Path) -> None:
    """Telegram chat ids fit in 52 bits; 17+ digits can overflow Alertmanager's int64 chat_id."""
    token = "123456:not-a-real-token-config-check-only"
    code, out, _ = _render_alertmanager(tmp_path, token, "12345678901234567890", "")
    assert code == 0 and "telegram receiver: off" in out, out
    code, out, _ = _render_alertmanager(tmp_path / "ok", token, "-1001234567890123", "")
    assert code == 0 and "telegram receiver: on" in out, out


def _redact_ips(line: str) -> str:
    """Apply the IPv4 then the IPv6 stage.replace of config.alloy the way Alloy does: every match,
    no overlap, and only the capture group is replaced (Python re agrees with RE2 on this syntax)."""
    alloy = (DEPLOY / "monitoring" / "alloy" / "config.alloy").read_text(encoding="utf-8")
    expressions = re.findall(r'expression = `([^`]*)`\s*replace\s*= "<ip>"', alloy)
    assert len(expressions) == 2, "IPv4 and IPv6 stages"
    for expression in expressions:
        parts, last = [], 0
        for m in re.finditer(expression, line):
            parts += [line[last : m.start(1)], "<ip>"]
            last = m.end(1)
        line = "".join([*parts, line[last:]])
    return line


def test_ip_redaction_leaves_sql_casts_in_tracebacks_readable() -> None:
    """The worker logs whole tracebacks; asyncpg renders every bind as "$1::UUID". The IPv6 stage
    turned them into "$<ip>OOLEAN" and the like."""
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import asyncpg

    table = sa.Table(
        "t",
        sa.MetaData(),
        sa.Column("a", sa.Uuid),
        sa.Column("b", sa.String),
        sa.Column("c", sa.Boolean),
        sa.Column("d", sa.DateTime(timezone=True)),
        sa.Column("e", sa.Date),
        sa.Column("f", sa.BigInteger),
    )
    values = {"a": None, "b": "", "c": True, "d": None, "e": None, "f": 1}
    sql = str(sa.insert(table).values(**values).compile(dialect=asyncpg.dialect()))
    assert "$3::BOOLEAN" in sql and "$5::DATE" in sql, sql
    for line in (
        f"[SQL: {sql}]",
        "SELECT n.id::text, value::text, gen_random_uuid()::text, (now() AT TIME ZONE 'UTC')::date",
        "SELECT pg_try_advisory_lock($1::INTEGER) 2026-11-12 10:11:12,345 INFO worker",
    ):
        assert _redact_ips(line) == line


@pytest.mark.parametrize(
    ("line", "redacted"),
    [
        ("client 2a01:4f8:c0c:1234::1 connected", "client <ip> connected"),
        ("ip:2a01:4f8::1", "ip:<ip>"),
        ("2a01::1,2a01::2", "<ip>,<ip>"),
        ("[2001:db8::1]:443", "[<ip>]:443"),
        ("::1", "<ip>"),
        ("fe80::1%eth0", "<ip>%eth0"),
        ("::ffff:10.0.0.1", "<ip>:<ip>"),
        ("2001:0db8:85a3:0000:0000:8a2e:0370:7334 x", "<ip> x"),
        ("route 2a01:4f8:c0c::/64", "route <ip>/64"),
        ("x=fe80::abcd:1;", "x=<ip>;"),
        ('{"ip": "2a01:4f8::2"}', '{"ip": "<ip>"}'),
        ("GET /?host=%5B2a01:4f8::1%5D", "GET /?host=%5B<ip>%5D"),
        ("out='a\\n2a01:4f8::1 dev eth0'", "out='a\\n<ip> dev eth0'"),
        ("from 10.0.0.1:8000 and 192.168.1.20", "from <ip>:8000 and <ip>"),
    ],
)
def test_ip_redaction_still_catches_every_address_form(line: str, redacted: str) -> None:
    """Rule 7, second line of defence: the cast-safe boundary must not let an address through."""
    assert _redact_ips(line) == redacted


SECRETISH_ENV = re.compile(r"PASS(WORD)?|TOKEN|SECRET|API_?KEY|PRIVATE", re.IGNORECASE)


def test_no_secret_is_passed_through_any_service_environment() -> None:
    """P05 note: alloy reads /var/lib/docker/containers read-only, and each container's
    config.v2.json there holds its environment. So a secret must never reach a container by
    environment variable, only as a ``*_FILE`` path to a mounted secret under /run/secrets/."""
    checked = 0
    for name, svc in _compose()["services"].items():
        env = svc.get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
        for key, value in env.items():
            checked += 1
            if SECRETISH_ENV.search(key):
                assert key.endswith("_FILE"), f"{name}: {key} must be a *_FILE secret path"
                assert str(value).startswith("/run/secrets/"), f"{name}: {key} must point into /run/secrets/"
    assert checked >= 5  # the check really saw the services' environments
    assert SECRETISH_ENV.search("REDIS_PASSWORD") and SECRETISH_ENV.search("bot_token")

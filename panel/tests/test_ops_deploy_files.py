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

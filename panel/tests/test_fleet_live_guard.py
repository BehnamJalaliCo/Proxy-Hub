"""Guards of the live fleet (round 3).

* The fleet loop is live ONLY when the owner turned dry-run off, the kill switch is off and the
  stored settings are valid; ``FLEET_LIVE_ENABLED`` remains a build-time emergency lock.
* A dry-run plan is never applied, by either apply path.
* No fleet-team module reaches the Hetzner mutation module or the permits: the fleet loop only
  writes rows; the node executor alone creates and deletes servers (tests/test_live_units.py).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from proxyhub.fleet.live import apply_plan
from proxyhub.fleet.planner import FleetPlan, PlannedAction
from proxyhub.reconcilers import fleet as fleet_loop
from proxyhub.settings_schema import FleetSettings

PANEL = Path(__file__).resolve().parents[1] / "proxyhub"
FLEET_TEAM_MODULES = [
    PANEL / "vault.py",
    *sorted((PANEL / "accounts").rglob("*.py")),
    *sorted((PANEL / "fleet").rglob("*.py")),
    PANEL / "reconcilers" / "account_guard.py",
    PANEL / "reconcilers" / "pricing.py",
    PANEL / "reconcilers" / "fleet.py",
    PANEL / "cloudflare" / "delegation.py",
    PANEL / "api" / "routes_accounts.py",
    PANEL / "api" / "routes_settings.py",
    PANEL / "api" / "routes_overview.py",
    PANEL / "api" / "routes_fleet_live.py",
    PANEL / "worker.py",
]
FORBIDDEN_NAMES = {"create_server", "delete_server", "issue_create_permit", "issue_delete_permit"}
OPEN = {"dry_run": False, "kill_switch": False}


def test_live_mode_needs_dry_run_off_the_kill_switch_off_and_valid_settings() -> None:
    assert fleet_loop.FLEET_LIVE_ENABLED is True
    live = fleet_loop.live_mode
    assert live(FleetSettings()) is False  # defaults: kill switch on, dry-run on
    assert live(FleetSettings(dry_run=False)) is False  # the kill switch starts on
    assert live(FleetSettings(kill_switch=False)) is False  # still dry-run
    assert live(FleetSettings(**OPEN)) is True
    assert live(FleetSettings(**OPEN), "settings_invalid") is False


def test_the_build_lock_forces_every_plan_dry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_loop, "FLEET_LIVE_ENABLED", False)
    assert fleet_loop.live_mode(FleetSettings(**OPEN)) is False


def _plan(dry_run: bool) -> FleetPlan:
    create = PlannedAction("create", "slot-1", True, (), {"slot": 1})
    return FleetPlan(datetime.now(UTC), dry_run, (create,), Decimal(0), Decimal(0), 0)


async def test_a_dry_run_plan_is_never_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(fleet_loop.LiveModeDisabled):
        await fleet_loop.execute_plan(None, _plan(True))  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        await apply_plan(None, _plan(True), datetime.now(UTC))  # type: ignore[arg-type]
    monkeypatch.setattr(fleet_loop, "FLEET_LIVE_ENABLED", False)
    with pytest.raises(fleet_loop.LiveModeDisabled):
        await fleet_loop.execute_plan(None, _plan(False))  # type: ignore[arg-type]
    assert issubclass(fleet_loop.LiveModeDisabled, PermissionError)


def test_no_fleet_module_imports_hetzner_mutations_or_permits() -> None:
    assert all(p.exists() for p in FLEET_TEAM_MODULES)
    assert PANEL / "fleet" / "live.py" in FLEET_TEAM_MODULES
    offenders: list[str] = []
    for path in FLEET_TEAM_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "mutations" in node.module:
                offenders.append(f"{path.name}: from {node.module}")
            if isinstance(node, ast.Import) and any("mutations" in a.name for a in node.names):
                offenders.append(f"{path.name}: import mutations")
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name in FORBIDDEN_NAMES:
                    offenders.append(f"{path.name}: uses {name}")
    assert not offenders, offenders


def test_negative_control_detects_an_import() -> None:
    tree = ast.parse("from proxyhub.hetzner.mutations import create_server\ncreate_server(r, p, s)\n")
    found = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and "mutations" in (n.module or "")]
    names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES]
    assert found and names

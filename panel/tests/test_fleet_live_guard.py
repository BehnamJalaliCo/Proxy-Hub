"""The fleet loop is DRY-RUN ONLY in this milestone. Turning it live is a later owner decision.

These tests FAIL if FLEET_LIVE_ENABLED is switched on, if any fleet-team module reaches the
Hetzner mutation module, or if the live path can be entered.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from proxyhub.fleet.planner import FleetPlan
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
    PANEL / "api" / "routes_accounts.py",
    PANEL / "api" / "routes_settings.py",
    PANEL / "api" / "routes_overview.py",
    PANEL / "worker.py",
]
FORBIDDEN_NAMES = {"create_server", "delete_server", "issue_create_permit", "issue_delete_permit"}


def test_fleet_live_mode_is_off_in_this_milestone() -> None:
    assert fleet_loop.FLEET_LIVE_ENABLED is False, (
        "turning the fleet live is an owner decision (later milestone)"
    )


def test_dry_run_false_does_not_make_the_loop_live() -> None:
    assert fleet_loop.live_mode(FleetSettings(dry_run=False, kill_switch=False)) is False
    assert fleet_loop.live_mode(FleetSettings()) is False


async def test_mutation_path_is_closed() -> None:
    plan = FleetPlan(datetime.now(UTC), False, (), Decimal(0), Decimal(0), 0)
    with pytest.raises(fleet_loop.LiveModeDisabled):
        await fleet_loop.execute_plan(None, plan)  # type: ignore[arg-type]
    with pytest.raises(fleet_loop.LiveModeDisabled):
        await fleet_loop.execute_plan(None, FleetPlan(datetime.now(UTC), True, (), Decimal(0), Decimal(0), 0))  # type: ignore[arg-type]


def test_no_fleet_module_imports_hetzner_mutations_or_permits() -> None:
    assert all(p.exists() for p in FLEET_TEAM_MODULES)
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

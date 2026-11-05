"""P15 on real PostgreSQL: the candidate sites' rounds from Iran, our nodes' own checks of them, the
``auto`` switches of the Reality target and the MTProto fake-TLS domain, the targets loop, and the
API. Globalping answers are the real recorded shapes in tests/test_net_support.py (D10); the loops
are pointed at a closed local port where a call would leave the sandbox.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF
from test_net_support import (
    HTTP_OK_GITHUB_RIGHTEL,
    HTTP_TLS_TIMEOUT_RIGHTEL_443,
    MCI_PROBE,
    RIGHTEL_PROBE,
    actions,
    clean_net_tables,
    gp_https,
    make_node,
)

from proxyhub.api.deps import require_admin
from proxyhub.mtproto.settings import MtprotoSettings, load_settings, save_settings
from proxyhub.probes.globalping import GlobalpingClient, RateLimit, parse_http
from proxyhub.probes.types import Measurement
from proxyhub.reality.settings import RealitySettings, load_reality_settings, save_reality_settings
from proxyhub.reconcilers.targets import TargetsReconciler
from proxyhub.targets.service import (
    TargetRound,
    apply_auto,
    load_candidates,
    record_node_checks,
    store_round,
)
from proxyhub.targets.settings import TargetSettings, load_state, save_target_settings
from proxyhub.telegram.store import set_owner_chat

pytestmark = [pytest.mark.db]

KEYS = ["asn:197207", "asn:57218"]
KEY_ASNS = [197207, 57218]
CLOSED = "http://127.0.0.1:9"


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean(eng)
    yield eng
    await clean(eng)
    await eng.dispose()


async def clean(eng: AsyncEngine) -> None:
    await clean_net_tables(eng)
    async with eng.begin() as conn:
        await conn.execute(
            text("DELETE FROM settings WHERE key IN ('reality', 'mtproto', 'fleet', 'telegram.owner')")
        )
        await conn.execute(text("DELETE FROM fleet_alerts"))
        await conn.execute(text("DELETE FROM outbox"))


def closed_gp() -> GlobalpingClient:
    return GlobalpingClient(base_url=CLOSED, poll_seconds=0.01, max_wait_seconds=0.1)


# --- candidate sites: rounds, node checks, auto switches ------------------------------------------------


def https(raw: dict[str, Any], role: str, target: str, at: datetime) -> Measurement:
    return Measurement(
        "globalping", "https", role, target, 443, "ok", at,  # type: ignore[arg-type]
        ref=raw["id"], networks=tuple(parse_http(raw, KEY_ASNS)),
    )  # fmt: skip


OK = HTTP_OK_GITHUB_RIGHTEL


def timeout(address: str) -> dict[str, Any]:
    return {**HTTP_TLS_TIMEOUT_RIGHTEL_443, "resolvedAddress": address}


def site_round(at: datetime, open_sites: list[str], mci_closed: list[str]) -> TargetRound:
    """A round assembled from real result objects: the control and ``open_sites`` answer 200 on MCI
    and Rightel; ``mci_closed`` time out in the TLS handshake on MCI and answer on Rightel."""
    control = https(
        gp_https("c1", "github.com", 443, [(MCI_PROBE, OK), (RIGHTEL_PROBE, OK)], at="x"),
        "control",
        "github.com",
        at,
    )
    results: dict[str, Measurement] = {}
    for host in open_sites:
        results[host] = https(
            gp_https("o", host, 443, [(MCI_PROBE, OK), (RIGHTEL_PROBE, OK)], at="x"), "target", host, at
        )
    for host in mci_closed:
        raw = gp_https("b", host, 443, [(MCI_PROBE, timeout("23.36.157.47")), (RIGHTEL_PROBE, OK)], at="x")
        results[host] = https(raw, "target", host, at)
    return TargetRound(
        at, at + timedelta(seconds=20), control, results, RateLimit(250, 200, at + timedelta(minutes=40))
    )


async def test_rounds_and_node_checks_are_stored_per_site(engine: AsyncEngine) -> None:
    t1 = datetime.now(UTC) - timedelta(hours=7)
    async with engine.begin() as conn:
        assert await store_round(conn, site_round(t1, ["www.apple.com"], ["www.microsoft.com"])) == [
            "www.apple.com",
            "www.microsoft.com",
        ]
        await store_round(conn, site_round(t1 + timedelta(hours=6), ["www.apple.com"], ["www.microsoft.com"]))
    async with engine.connect() as conn:
        states = await load_candidates(conn)
        stored = (
            await conn.execute(
                text("SELECT kind, role, count(*) FROM probe_measurements GROUP BY kind, role ORDER BY role")
            )
        ).all()
    ms = states["www.microsoft.com"]
    assert ms.iran["asn:197207"].state == "down" and ms.iran_prev["asn:197207"].state == "down"
    assert ms.iran["asn:57218"].state == "up" and ms.iran["asn:57218"].ms == 548.0
    assert (
        ms.prev_measured_at is not None
        and ms.measured_at is not None
        and ms.measured_at > ms.prev_measured_at
    )
    assert [tuple(r) for r in stored] == [("https", "control", 2), ("https", "target", 4)]

    node_a = await make_node(engine, state="ready", ipv4="203.0.113.80")
    node_b = await make_node(engine, state="ready", ipv4="203.0.113.81")
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        for node_id, ok in ((node_a, True), (node_b, False)):
            await conn.execute(
                text(
                    "INSERT INTO reality_node_status (node_id, enabled, up, target_ok, checked_target, "
                    "checked_server_name, users_reported, reported_at) VALUES (:n, true, true, :ok, "
                    "'www.apple.com:443', 'www.apple.com', 0, :at)"
                ),
                {"n": node_id, "ok": ok, "at": now},
            )
        # One node passes the site and one fails it: passed (configs go on the nodes that pass).
        assert await record_node_checks(conn, ["www.apple.com", "github.com"], now) == {
            "www.apple.com": "passed"
        }
        await conn.execute(text("UPDATE reality_node_status SET target_ok = false"))
        later = now + timedelta(minutes=1)
        assert await record_node_checks(conn, ["www.apple.com"], later) == {"www.apple.com": "failed"}
        # A report older than an hour does not count.
        assert await record_node_checks(conn, ["www.apple.com"], now + timedelta(hours=2)) == {}
    async with engine.connect() as conn:
        apple = (await load_candidates(conn))["www.apple.com"]
    assert apple.node_ok_at == now and apple.node_fail_at == later
    assert apple.iran["asn:197207"].state == "up"  # its Iranian rounds are untouched


async def test_auto_mode_switches_targets_logs_and_tells_the_owner(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await set_owner_chat(conn, 424242, now)
        await store_round(
            conn, site_round(now - timedelta(hours=7), ["www.apple.com", "github.com"], ["www.microsoft.com"])
        )
        await store_round(
            conn, site_round(now - timedelta(hours=1), ["www.apple.com"], ["www.microsoft.com"])
        )
        switched = await apply_auto(conn, TargetSettings(), KEYS, now)
    assert [(s.kind, s.old, s.new, s.why) for s in switched] == [
        ("reality", "dl.google.com", "www.apple.com", "not_a_candidate"),
        ("mtproto", "www.microsoft.com", "www.apple.com", "blocked_from_iran"),
    ]
    async with engine.connect() as conn:
        reality = await load_reality_settings(conn)
        mt = await load_settings(conn)
        alerts = (
            (await conn.execute(text("SELECT payload FROM outbox WHERE kind = 'alert' ORDER BY id")))
            .scalars()
            .all()
        )
    assert (reality.target_host, reality.target_port, reality.target_mode) == ("www.apple.com", 443, "auto")
    assert (mt.tls_domain, mt.port, mt.target_mode) == ("www.apple.com", 8443, "auto")
    assert len(alerts) == 2 and "Reality" in alerts[0]["text"] and "MTProto" in alerts[1]["text"]
    assert "همراه اول" in alerts[1]["text"] and "D42" in alerts[1]["text"]
    log = [
        (a["action"], a["target"], a["reasons"]["context"]["from"], a["reasons"]["context"]["to"])
        for a in await actions(engine)
    ]
    assert log == [
        ("target_auto_switched", "reality", "dl.google.com", "www.apple.com"),
        ("target_auto_switched", "mtproto", "www.microsoft.com", "www.apple.com"),
    ]
    # Settled: the next pass changes nothing.
    async with engine.begin() as conn:
        assert await apply_auto(conn, TargetSettings(), KEYS, now) == []


async def test_manual_mode_never_switches_and_no_open_site_is_told_daily(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await set_owner_chat(conn, 424242, now)
        await save_reality_settings(conn, RealitySettings(target_mode="manual"))
        await save_settings(conn, MtprotoSettings(target_mode="manual"))
        for at in (now - timedelta(hours=7), now - timedelta(hours=1)):
            await store_round(conn, site_round(at, ["www.apple.com"], ["www.microsoft.com"]))
        assert await apply_auto(conn, TargetSettings(), KEYS, now) == []
    async with engine.connect() as conn:
        assert (await load_reality_settings(conn)).target_host == "dl.google.com"
        assert (await load_settings(conn)).tls_domain == "www.microsoft.com"
    # Auto, but every candidate is closed on MCI too: nothing to switch to; the owner hears once a day.
    async with engine.begin() as conn:
        await save_settings(conn, MtprotoSettings())
        await conn.execute(text("DELETE FROM target_states"))
        for at in (now - timedelta(hours=7), now - timedelta(hours=1)):
            await store_round(conn, site_round(at, [], ["www.microsoft.com", "www.apple.com"]))
        only = TargetSettings(candidates=["www.apple.com"])
        assert await apply_auto(conn, only, KEYS, now) == []
        assert await apply_auto(conn, only, KEYS, now + timedelta(hours=1)) == []
        await store_round(
            conn, site_round(now + timedelta(hours=20), [], ["www.microsoft.com", "www.apple.com"])
        )
        assert await apply_auto(conn, only, KEYS, now + timedelta(hours=25)) == []
    async with engine.connect() as conn:
        assert (await load_settings(conn)).tls_domain == "www.microsoft.com"
        alerts = (await conn.execute(text("SELECT payload FROM outbox WHERE kind = 'alert'"))).scalars().all()
    assert len(alerts) == 2 and all("دامنه‌ی TLS پروکسی MTProto" in a["text"] for a in alerts)
    assert [a["action"] for a in await actions(engine)] == ["target_no_candidate", "target_no_candidate"]


async def test_the_targets_loop_with_globalping_unreachable(engine: AsyncEngine) -> None:
    rec = TargetsReconciler(gp_factory=closed_gp)
    async with engine.begin() as conn:
        await rec.run_once(conn)
    async with engine.connect() as conn:
        state = await load_state(conn)
        statuses = (await conn.execute(text("SELECT DISTINCT status, role FROM probe_measurements"))).all()
        assert (
            await load_reality_settings(conn)
        ).target_host == "dl.google.com"  # nothing measured: no switch
    assert "last_attempt_at" in state and "last_full_at" not in state  # the control failed: retried later
    assert {tuple(s) for s in statuses} == {("error", "control"), ("error", "target")}
    async with engine.begin() as conn:
        await rec.run_once(conn)  # too soon for another attempt: nothing called
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM probe_cycles"))).scalar_one() == 1
    async with engine.begin() as conn:
        await save_target_settings(conn, TargetSettings(enabled=False))
        await conn.execute(text("DELETE FROM settings WHERE key = 'targets.state'"))
    async with engine.begin() as conn:
        await rec.run_once(conn)  # switched off: nothing at all
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM probe_cycles"))).scalar_one() == 1


async def test_the_targets_loop_keeps_the_allowance_for_the_node_probes(engine: AsyncEngine) -> None:
    await make_node(engine, state="ready", ipv4="203.0.113.90")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO probe_sources (name, ok, note, checked_at, ratelimit_limit, "
                "ratelimit_remaining, ratelimit_reset_at, tls_probes) "
                "VALUES ('globalping', true, 'x', now(), 250, 58, "
                "now() + interval '20 minutes', 3)"
            )
        )
    async with engine.begin() as conn:
        await TargetsReconciler(gp_factory=closed_gp).run_once(conn)
    # 5 probes x (the control + 9 sites; github.com is the control itself) = 50, plus 3 x 3 = 9 kept
    # for one node's TLS cycle: 59 > 58, postponed.
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM probe_cycles"))).scalar_one() == 0
        assert await load_state(conn) == {}


def test_worker_runs_the_targets_loop_once() -> None:
    from proxyhub.worker import reconcilers

    assert sum(isinstance(r, TargetsReconciler) for r in reconcilers()) == 1


# --- API ------------------------------------------------------------------------------------------------


TARGET_PATHS = {("GET", "/api/targets"), ("PUT", "/api/targets/settings")}


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        app.dependency_overrides.pop(require_admin, None)


@pytest.mark.redis
async def test_every_target_route_requires_admin(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_targets
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_targets.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            found.add((method, route.path))
            assert require_admin in {d.call for d in route.dependant.dependencies}
    assert found == TARGET_PATHS
    app.dependency_overrides.pop(require_admin, None)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(TARGET_PATHS):
                r = await c.request(method, path, json={})
                assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


@pytest.mark.redis
async def test_targets_over_http(client: AsyncClient, engine: AsyncEngine) -> None:
    body = (await client.get("/api/targets")).json()
    assert body["settings"] == body["defaults"] and body["key_networks"] == KEYS
    assert body["reality"] == {"mode": "auto", "target": "dl.google.com", "transport": "ws_tls"}
    assert body["mtproto"] == {"mode": "auto", "tls_domain": "www.microsoft.com"}
    hosts = [c["host"] for c in body["candidates"]]
    assert hosts[:8] == body["settings"]["candidates"] and hosts[8:] == ["dl.google.com", "www.microsoft.com"]
    (dl,) = [c for c in body["candidates"] if c["host"] == "dl.google.com"]
    assert dl == {
        "host": "dl.google.com",
        "listed": False,
        "current_for": ["reality"],
        "measured_at": None,
        "networks": [],
        "prev_measured_at": None,
        "prev_networks": [],
        "open": False,
        "blocked_on": [],
        "ms": None,
        "node_check": None,
        "node_ok_at": None,
        "node_fail_at": None,
    }
    async with engine.begin() as conn:
        now = datetime.now(UTC)
        await store_round(conn, site_round(now - timedelta(hours=7), [], ["www.microsoft.com"]))
        await store_round(
            conn, site_round(now - timedelta(hours=1), ["www.apple.com"], ["www.microsoft.com"])
        )
    body = (await client.get("/api/targets")).json()
    (ms,) = [c for c in body["candidates"] if c["host"] == "www.microsoft.com"]
    assert ms["blocked_on"] == ["asn:197207"] and ms["open"] is False
    assert [(n["key"], n["state"], n["key_network"]) for n in ms["networks"]] == [
        ("asn:197207", "down", True),
        ("asn:57218", "up", True),
    ]
    (apple,) = [c for c in body["candidates"] if c["host"] == "www.apple.com"]
    assert apple["open"] is True and apple["ms"] == 548.0

    r = await client.put(
        "/api/targets/settings", json={"candidates": ["WWW.Apple.com", "github.com"], "interval_h": 4}
    )
    assert r.status_code == 200, r.text
    assert r.json()["settings"] == {
        "enabled": True,
        "candidates": ["www.apple.com", "github.com"],
        "interval_h": 4,
    }
    (log,) = [a for a in await actions(engine) if a["action"] == "settings_update"]
    assert log["target"] == "targets" and log["reasons"]["context"]["changed"]["interval_h"] == [6, 4]
    for bad in ({"candidates": ["1.1.1.1"]}, {"candidates": []}, {"interval_h": 0}, {"extra": 1}, [1]):
        r = await client.put("/api/targets/settings", json=bad)
        assert r.status_code == 422 and r.json()["error"] == "invalid_settings" and r.json()["message_fa"]
        assert "1.1.1.1" not in r.text


@pytest.mark.redis
async def test_the_reality_target_mode_is_a_setting(client: AsyncClient, engine: AsyncEngine) -> None:
    payload = RealitySettings().model_dump(mode="json")
    assert payload["target_mode"] == "auto"
    r = await client.put(
        "/api/reality/settings", json={**payload, "target_mode": "manual", "target_host": "www.apple.com"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_mode"] == "manual" and r.json()["target_host"] == "www.apple.com"
    r = await client.put("/api/reality/settings", json={**payload, "target_mode": "sometimes"})
    assert r.status_code == 422 and r.json()["fields"] == ["target_mode"]

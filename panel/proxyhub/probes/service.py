"""One probe cycle (network I/O), its storage with verdicts, and the dashboard summary.

A cycle (P15, 2026-11-05: only a real TLS handshake tells the truth from Iran):

1. **TLS, the part that decides.** One Globalping ``http`` measurement (HTTPS ``HEAD /``) of the TLS
   control host from MCI, Rightel and other Iranian networks; then, reusing exactly its probes, one
   per due node on 443, on the node's own DNS name when its record points at the node (the SNI its
   links use), else on its IP. The MTProto port is NOT measured by TLS: Telemt answers only its own
   fake-TLS domain (P16: SNI = the node's name gets no answer even when the port is open), and
   Globalping cannot send another SNI to our address; it shares the node's address with 443. A
   cycle takes only as many nodes as Globalping's remaining hourly allowance covers
   (:func:`tls_fit`); the others stay due.
   A node is measured on schedule only :data:`READY_GRACE` after it became READY, so its first
   round sees the configs it was given (P16: a round a minute too early counted as "closed").
2. **Information only** (on what the allowance leaves): the earlier Globalping TCP ping of the
   MTProto port with its own TCP control, ICMP, and a DNS check of the node's slot name; check-host
   TCP from its Iranian nodes (no Globalping cost). MCI and Rightel answer a TCP SYN themselves for
   blocked addresses, so none of these ever makes a node reachable.

Results are stored per measurement and per network; each node's verdict is recomputed from its
stored rounds (proxyhub/probes/verdict.py, source ``https``). A changed verdict is written to
actions_log. The live fleet (proxyhub/fleet/live.py) burns a READY slot node with a fresh
``blocked`` verdict only when its own agent says the probed service is up. Every round also
updates the node's row in ``probe_location_reach`` (the fleet's location scores).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.cloudflare.service import NodeNames
from proxyhub.db.models import Node
from proxyhub.db.models_agent import TelemtNodeConfig
from proxyhub.db.models_net import (
    ProbeCycle,
    ProbeLocationReach,
    ProbeMeasurement,
    ProbeNetworkResult,
    ProbeNodeState,
    ProbeRound,
    ProbeSource,
)
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.probes.checkhost import CheckHostClient, CheckHostNode
from proxyhub.probes.globalping import (
    GlobalpingClient,
    RateLimit,
    dns_body,
    https_body,
    icmp_body,
    location_asns,
    tcp_body,
)
from proxyhub.probes.settings import ProbeSettings
from proxyhub.probes.types import Measurement, MeasurementStatus
from proxyhub.probes.verdict import (
    SOURCES,
    TLS_SOURCE,
    TLS_SOURCES,
    RoundObs,
    SourceObservation,
    control_ok,
    decide,
    observe,
    observe_tls,
)

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
GpFactory = Callable[[], GlobalpingClient]
ChFactory = Callable[[], CheckHostClient]

ACTOR = "probes"
GP_CONCURRENCY = 4
CH_CONCURRENCY = 2
TLS_PORT = 443  # Xray (VLESS + WS + TLS, or Reality) on every node


class ProbeServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Target:
    node_id: uuid.UUID
    name: str
    ip: str
    port: int  # the MTProto (Telemt) port, or the default port without a Telemt config
    dns_name: str | None  # the slot name on the active domain (DNS hijack check)
    trigger: str  # 'schedule' | 'manual'
    # The node's own DNS name when its record points at exactly this node (the SNI its links use);
    # None: the TLS handshakes go to the IP.
    tls_name: str | None = None
    location: str = ""
    tls_ports: tuple[int, ...] = (TLS_PORT,)

    @property
    def tls_host(self) -> str:
        return self.tls_name or self.ip


@dataclass
class CycleResult:
    started_at: datetime
    finished_at: datetime
    controls: dict[str, Measurement]  # TCP controls per source (information)
    nodes: list[Measurement] = field(default_factory=list)
    gp_rate: RateLimit | None = None
    tls_control: Measurement | None = None
    # The targets this cycle measured (TLS); the others stay due for a later cycle.
    measured: list[Target] = field(default_factory=list)
    tls_probes: int | None = None  # probes the TLS control used (each one a Globalping test)


# --- choosing what to probe ---------------------------------------------------------------------


def _ipv4(value: Any) -> str | None:
    try:
        ip = ipaddress.ip_interface(str(value)).ip
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


# A READY node gets its configs applied within a minute or two (P16); the first scheduled round waits.
READY_GRACE: Final = timedelta(minutes=5)


async def due_targets(
    db: Db, settings: ProbeSettings, now: datetime, names: NodeNames | None
) -> list[Target]:
    """READY nodes whose last round is older than the interval, and nodes the owner asked for.
    ``names``: the node names Cloudflare holds on the active domain (None: no domain)."""
    cutoff = now - timedelta(seconds=settings.interval_s)
    rows = (
        await db.execute(
            select(
                Node.id,
                Node.name,
                Node.ipv4,
                Node.slot,
                Node.state,
                Node.location,
                ProbeNodeState.last_round_at,
                ProbeNodeState.run_requested_at,
                TelemtNodeConfig.ports,
            )
            .outerjoin(ProbeNodeState, ProbeNodeState.node_id == Node.id)
            .outerjoin(TelemtNodeConfig, TelemtNodeConfig.node_id == Node.id)
            .where(
                Node.ipv4.is_not(None),
                Node.state != NodeState.DELETED.value,
                (ProbeNodeState.run_requested_at.is_not(None))
                | (
                    (Node.state == NodeState.READY.value)
                    & (Node.ready_at.is_(None) | (Node.ready_at <= now - READY_GRACE))
                    & (ProbeNodeState.last_round_at.is_(None) | (ProbeNodeState.last_round_at <= cutoff))
                ),
            )
            .order_by(
                ProbeNodeState.run_requested_at.asc().nulls_last(),
                ProbeNodeState.last_round_at.asc().nulls_first(),
                Node.name,
            )
            .limit(settings.max_nodes_per_tick)
        )
    ).all()
    zone = names.zone if names is not None else None
    targets: list[Target] = []
    for r in rows:
        ip = _ipv4(r.ipv4)
        if ip is None:
            continue
        port = int(r.ports[0]) if r.ports else settings.default_port
        dns_name = f"slot{r.slot}.{zone}" if zone and r.slot and r.slot > 0 else None
        tls_name = names.own_name(r.slot, r.id, ip) if names is not None else None
        ports = (TLS_PORT,)  # see the module docstring: the MTProto port is not measured by TLS
        trigger = "manual" if r.run_requested_at is not None else "schedule"
        targets.append(Target(r.id, r.name, ip, port, dns_name, trigger, tls_name, r.location, ports))
    return targets


# --- the Globalping budget -------------------------------------------------------------------


def tls_cost(targets: Sequence[Target], probes: int) -> int:
    """Globalping tests of the TLS part of a cycle: the control and one measurement per node and
    port, each run by at most ``probes`` probes."""
    return probes * (1 + sum(len(t.tls_ports) for t in targets))


def tls_fit(
    targets: Sequence[Target], probes: int, rate: RateLimit | None, now: datetime, reserve: int = 0
) -> int:
    """How many of the due targets (in their order: owner requests first, then the longest
    unmeasured) one cycle can measure with TLS inside Globalping's remaining hourly allowance,
    keeping ``reserve`` tests. An unknown or expired window: all of them (the headers of the first
    measurement tell the truth again)."""
    if rate is None or rate.reset_at <= now:
        return len(targets)
    available = rate.remaining - reserve
    cost, fit = probes, 0
    for t in targets:
        cost += probes * len(t.tls_ports)
        if cost > available:
            break
        fit += 1
    return fit


def cycle_costs(settings: ProbeSettings, targets: int, dns_targets: int) -> tuple[int, int, int]:
    """Globalping tests of the information part of one cycle: (TCP control + TCP, ICMP, DNS). Each
    measurement costs at most one test per requested network."""
    per = len(settings.globalping_asns)
    core = per * (1 + targets)
    icmp = per * targets if settings.icmp else 0
    dns = per * dns_targets if settings.dns else 0
    return core, icmp, dns


def node_probe_reserve(settings: ProbeSettings, ready_nodes: int, tls_probes: int | None = None) -> int:
    """Tests to keep for the node probes' next TLS cycle (other Globalping users, such as the domain
    filter checks and the target candidates, stay below this so the node probes are never starved).
    ``tls_probes``: the probes the last TLS control really used (else the most it could use)."""
    if not settings.enabled or ready_nodes <= 0:
        return 0
    n = min(ready_nodes, settings.max_nodes_per_tick)
    probes = tls_probes if tls_probes is not None and tls_probes > 0 else settings.tls_probe_limit
    return probes * (1 + 2 * n)


def globalping_plan(
    settings: ProbeSettings, targets: Sequence[Target], rate: RateLimit | None, now: datetime
) -> tuple[bool, bool, bool]:
    """(run TCP at all, run ICMP, run DNS): the information measurements, only on what the
    allowance leaves (the TLS part has been paid for before this is asked)."""
    core, icmp, dns = cycle_costs(settings, len(targets), sum(1 for t in targets if t.dns_name))
    if rate is None or rate.reset_at <= now:
        return True, settings.icmp, settings.dns
    left = rate.remaining
    if left < core:
        return False, False, False
    run_icmp = settings.icmp and left >= core + icmp
    run_dns = settings.dns and left >= core + (icmp if run_icmp else 0) + dns
    return True, run_icmp, run_dns


# --- one cycle of measurements (network I/O only) ------------------------------------------------


async def _limited[R](sem: asyncio.Semaphore, coro: Awaitable[R]) -> R:
    async with sem:
        return await coro


def _skipped(
    source: str,
    kind: str,
    target: str,
    port: int | None,
    status: MeasurementStatus,
    node_id: uuid.UUID,
    at: datetime,
) -> Measurement:
    return Measurement(source, kind, "node", target, port, status, at, node_id=node_id)  # type: ignore[arg-type]


def probes_used(m: Measurement) -> int:
    """Probes that ran an ``https`` measurement (each adds exactly one ok, fail or nodata)."""
    return sum(n.ok + n.fail + n.nodata for n in m.networks)


async def run_cycle(
    targets: Sequence[Target],
    settings: ProbeSettings,
    *,
    gp_factory: GpFactory = GlobalpingClient,
    ch_factory: ChFactory = CheckHostClient,
    gp_rate: RateLimit | None = None,
    tls_probes: int | None = None,
) -> CycleResult | None:
    """Measure the due targets that the allowance covers. None: not even one node fits now (nothing
    was called; the targets stay due)."""
    started = datetime.now(UTC)
    estimate = tls_probes if tls_probes is not None and tls_probes > 0 else settings.tls_probe_limit
    if tls_fit(targets, estimate, gp_rate, started) == 0:
        return None
    tls_locations = settings.tls_location_bodies
    tls_asns = location_asns(tls_locations)
    asns = list(settings.globalping_asns)
    gp_sem, ch_sem = asyncio.Semaphore(GP_CONCURRENCY), asyncio.Semaphore(CH_CONCURRENCY)
    host, cport = settings.control_host, settings.control_port
    async with gp_factory() as gp, ch_factory() as ch:
        tls_ctrl, tls_ctrl_id = await gp.run(
            https_body(settings.tls_control_host, TLS_PORT, tls_locations),
            kind="https",
            role="control",
            target=settings.tls_control_host,
            port=TLS_PORT,
            requested_asns=tls_asns,
        )
        rate = gp.last_rate or gp_rate
        used = probes_used(tls_ctrl) if tls_ctrl.status == "ok" else None
        fit = tls_fit(targets, used or estimate, rate, datetime.now(UTC))
        measured = list(targets[:fit])
        # Reuse the control's exact probes; fall back to the location list only if the control
        # itself failed for a transient reason (so the node is still measured).
        tls_where: str | list[dict[str, Any]] | None
        if tls_ctrl_id is not None:
            tls_where = tls_ctrl_id
        elif tls_ctrl.status in ("error", "timeout"):
            tls_where = tls_locations
        else:
            tls_where = None

        tls_jobs: list[Awaitable[Measurement]] = []
        for t in measured:
            for port in t.tls_ports:
                if tls_where is None:
                    now = datetime.now(UTC)
                    tls_jobs.append(
                        _done(
                            _skipped("globalping", "https", t.tls_host, port, tls_ctrl.status, t.node_id, now)
                        )
                    )
                else:
                    tls_jobs.append(
                        _limited(
                            gp_sem,
                            _gp(
                                gp,
                                https_body(t.tls_host, port, tls_where),
                                "https",
                                t,
                                t.tls_host,
                                port,
                                tls_asns,
                            ),
                        )
                    )
        tls_results = list(await asyncio.gather(*tls_jobs))
        rate = gp.last_rate or rate

        # --- information only, on what the allowance leaves -------------------------------------
        run_gp, run_icmp, run_dns = globalping_plan(settings, measured, rate, datetime.now(UTC))

        async def gp_control() -> tuple[Measurement, str | None]:
            if not run_gp or not measured:
                return Measurement("globalping", "tcp", "control", host, cport, "rate_limited", started), None
            return await gp.run(
                tcp_body(host, cport, asns),
                kind="tcp",
                role="control",
                target=host,
                port=cport,
                requested_asns=asns,
            )

        async def ch_control() -> tuple[Measurement, list[CheckHostNode]]:
            if not settings.checkhost or not measured:
                return Measurement("check-host", "tcp", "control", host, cport, "skipped", started), []
            found = await ch.iran_nodes()
            if found is None:
                return Measurement("check-host", "tcp", "control", host, cport, "error", started), []
            nodes = found[: settings.checkhost_max_nodes]
            return await ch.tcp(host, cport, nodes, role="control"), nodes

        (gp_ctrl, gp_ctrl_id), (ch_ctrl, ch_nodes) = await asyncio.gather(gp_control(), ch_control())
        locations: str | list[int] | None
        if gp_ctrl_id is not None:
            locations = gp_ctrl_id
        elif gp_ctrl.status in ("error", "timeout"):
            locations = asns
        else:
            locations = None
        node_status: MeasurementStatus = gp_ctrl.status
        ch_status: MeasurementStatus = ch_ctrl.status if ch_ctrl.status != "ok" else "no_probes"

        jobs: list[Awaitable[Measurement]] = []
        for t in measured:
            now = datetime.now(UTC)
            if locations is None:
                jobs.append(_done(_skipped("globalping", "tcp", t.ip, t.port, node_status, t.node_id, now)))
            else:
                jobs.append(
                    _limited(gp_sem, _gp(gp, tcp_body(t.ip, t.port, locations), "tcp", t, t.ip, t.port, asns))
                )
                if run_icmp:
                    jobs.append(
                        _limited(gp_sem, _gp(gp, icmp_body(t.ip, locations), "icmp", t, t.ip, None, asns))
                    )
                if run_dns and t.dns_name:
                    jobs.append(
                        _limited(
                            gp_sem, _gp(gp, dns_body(t.dns_name, locations), "dns", t, t.dns_name, None, asns)
                        )
                    )
            if not ch_nodes:  # check-host off, unreachable, or without Iranian nodes
                jobs.append(_done(_skipped("check-host", "tcp", t.ip, t.port, ch_status, t.node_id, now)))
            else:
                jobs.append(_limited(ch_sem, ch.tcp(t.ip, t.port, ch_nodes, role="node", node_id=t.node_id)))
        node_results = list(await asyncio.gather(*jobs))
        rate = gp.last_rate or rate
    return CycleResult(
        started_at=started,
        finished_at=datetime.now(UTC),
        controls={"globalping": gp_ctrl, "check-host": ch_ctrl},
        nodes=[*tls_results, *node_results],
        gp_rate=rate,
        tls_control=tls_ctrl,
        measured=measured,
        tls_probes=used,
    )


async def _done(m: Measurement) -> Measurement:
    return m


async def _gp(
    gp: GlobalpingClient,
    body: dict[str, Any],
    kind: str,
    t: Target,
    target: str,
    port: int | None,
    asns: Sequence[int],
) -> Measurement:
    measurement, _ = await gp.run(
        body,
        kind=kind,  # type: ignore[arg-type]
        role="node",
        target=target,
        port=port,
        requested_asns=asns,
        expected_ip=t.ip if kind in ("dns", "https") else None,
        node_id=t.node_id,
    )
    return measurement


# --- storage and verdicts -----------------------------------------------------------------------


async def store_measurement(db: Db, cycle_id: int, m: Measurement) -> int:
    mid = (
        await db.execute(
            insert(ProbeMeasurement)
            .values(
                cycle_id=cycle_id,
                node_id=m.node_id if m.role == "node" else None,
                at=m.at,
                source=m.source,
                kind=m.kind,
                role=m.role,
                target=m.target[:253],
                port=m.port,
                status=m.status,
                ref=m.ref[:64] if m.ref else None,
                note=m.note[:200] if m.note else None,
            )
            .returning(ProbeMeasurement.id)
        )
    ).scalar_one()
    if m.networks:
        await db.execute(
            insert(ProbeNetworkResult),
            [
                {
                    "measurement_id": mid,
                    "network_key": n.network_key[:64],
                    "network": n.network[:100],
                    "asn": n.asn,
                    "ok": n.ok,
                    "fail": n.fail,
                    "nodata": n.nodata,
                    "rtt_ms": n.rtt_ms,
                    "detail": n.detail,
                }
                for n in m.networks
            ],
        )
    return int(mid)


async def _previous_rounds(db: Db, node_id: uuid.UUID, since: datetime, limit: int) -> list[RoundObs]:
    rows = (
        await db.execute(
            select(ProbeRound.started_at, ProbeRound.observations)
            .where(ProbeRound.node_id == node_id, ProbeRound.started_at >= since)
            .order_by(ProbeRound.started_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        RoundObs(
            at=r.started_at,
            observations={k: SourceObservation.from_json(v) for k, v in (r.observations or {}).items()},
        )
        for r in rows
    ]


def node_network_states(cells: Mapping[str, Mapping[str, str]]) -> dict[str, str]:
    """One round's TLS cells of a node -> per network ``open`` (a handshake worked on some port) or
    ``closed`` (every valid one failed). Networks without a valid cell are left out."""
    seen: dict[str, list[str]] = {}
    for port_cells in cells.values():
        for key, state in port_cells.items():
            if state in ("up", "down"):
                seen.setdefault(key, []).append(state)
    return {key: "open" if "up" in states else "closed" for key, states in seen.items()}


async def _store_location_reach(
    db: Db, t: Target, obs: SourceObservation, names: Mapping[str, tuple[str, int | None]], at: datetime
) -> None:
    if not t.location:
        return
    for key, state in sorted(node_network_states(obs.cells).items()):
        network, asn = names.get(key, (key, None))
        values = {"network": network[:100], "asn": asn, "location": t.location[:20], "state": state, "at": at}
        stmt = insert(ProbeLocationReach).values(node_id=t.node_id, network_key=key[:64], **values)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[ProbeLocationReach.node_id, ProbeLocationReach.network_key], set_=values
            )
        )


async def store_cycle(
    db: Db, result: CycleResult, targets: Sequence[Target], settings: ProbeSettings
) -> dict[uuid.UUID, tuple[str, list[str]]]:
    """Store everything, compute each measured node's verdict, record source health. Returns
    verdicts. ``targets``: the measured ones (``result.measured`` when set)."""
    cycle_id = (
        await db.execute(
            insert(ProbeCycle)
            .values(started_at=result.started_at, finished_at=result.finished_at)
            .returning(ProbeCycle.id)
        )
    ).scalar_one()
    if result.tls_control is not None:
        await store_measurement(db, cycle_id, result.tls_control)
    for control in result.controls.values():
        await store_measurement(db, cycle_id, control)
    tcp_by_node: dict[uuid.UUID, dict[str, Measurement]] = {}
    tls_by_node: dict[uuid.UUID, list[Measurement]] = {}
    network_names: dict[str, tuple[str, int | None]] = {}
    for m in result.nodes:
        await store_measurement(db, cycle_id, m)
        if m.node_id is None:
            continue
        if m.kind == "tcp":
            tcp_by_node.setdefault(m.node_id, {})[m.source] = m
        elif m.kind == "https":
            tls_by_node.setdefault(m.node_id, []).append(m)
            for n in m.networks:
                network_names.setdefault(n.network_key, (n.network, n.asn))

    now = result.finished_at
    max_age = timedelta(seconds=settings.max_round_age_s)
    verdicts: dict[uuid.UUID, tuple[str, list[str]]] = {}
    for t in result.measured or targets:
        node_tcp = tcp_by_node.get(t.node_id, {})
        observations = {s: observe(node_tcp.get(s), result.controls.get(s)) for s in SOURCES}
        tls_obs = observe_tls(tls_by_node.get(t.node_id, []), result.tls_control, settings.key_networks)
        observations[TLS_SOURCE] = tls_obs
        previous = await _previous_rounds(db, t.node_id, now - max_age, settings.min_rounds - 1)
        current = RoundObs(at=result.started_at, observations=observations)
        verdict, reasons = decide(
            [current, *previous],
            now=now,
            min_rounds=settings.min_rounds,
            max_age=max_age,
            sources=TLS_SOURCES,
        )
        await db.execute(
            insert(ProbeRound).values(
                cycle_id=cycle_id,
                node_id=t.node_id,
                started_at=result.started_at,
                finished_at=result.finished_at,
                trigger=t.trigger,
                target_ip=t.ip,
                port=t.port,
                observations={s: o.to_json() for s, o in observations.items()},
                verdict=verdict,
                verdict_reasons=reasons,
            )
        )
        await _store_location_reach(db, t, tls_obs, network_names, result.started_at)
        old = (
            await db.execute(select(ProbeNodeState.verdict).where(ProbeNodeState.node_id == t.node_id))
        ).scalar_one_or_none()
        stmt = insert(ProbeNodeState).values(
            node_id=t.node_id,
            last_round_at=result.started_at,
            run_requested_at=None,
            verdict=verdict,
            verdict_at=now,
            verdict_reasons=reasons,
        )
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[ProbeNodeState.node_id],
                set_={
                    "last_round_at": stmt.excluded.last_round_at,
                    "verdict": stmt.excluded.verdict,
                    "verdict_at": stmt.excluded.verdict_at,
                    "verdict_reasons": stmt.excluded.verdict_reasons,
                    # A request made while this cycle ran stays pending for the next one.
                    "run_requested_at": text(
                        "CASE WHEN probe_node_state.run_requested_at <= excluded.last_round_at "
                        "THEN NULL ELSE probe_node_state.run_requested_at END"
                    ),
                },
            )
        )
        if (old or "unknown") != verdict:
            await log_action(
                db,
                actor=ACTOR,
                action="probe_verdict",
                target=t.name,
                dry_run=False,
                allowed=True,
                reasons=[verdict],
                context={
                    "node_id": str(t.node_id),
                    "from": old or "unknown",
                    "to": verdict,
                    "reasons": reasons,
                    "port": t.port,
                    "tls_target": t.tls_host,
                    "tls_ports": list(t.tls_ports),
                },
                result=(
                    f"TLS reachability from Iran: {old or 'unknown'} -> {verdict} (the live fleet burns a "
                    "blocked slot node only when its agent says the service is up)"
                ),
            )
        verdicts[t.node_id] = (verdict, reasons)
    await _store_sources(db, result)
    return verdicts


SOURCE_NOTES_FA: dict[str, str] = {
    "rate_limited": "سقف رایگان ساعتی این سرویس پر شده است؛ سنجش پس از باز شدن سقف ادامه می‌یابد.",
    "no_probes": "در شبکه‌های خواسته‌شده هیچ نقطه‌ی سنجش آنلاینی نبود.",
    "error": "پاسخ این سرویس به دست نیامد.",
    "timeout": "این سرویس در زمان مقرر پاسخ کامل نداد.",
    "skipped": "در این دور اجرا نشد.",
    "control_failed": "هدف کنترل از هیچ شبکه‌ای در دسترس نبود؛ نتیجه‌های این دور معتبر شمرده نمی‌شوند.",
}


def source_health(control: Measurement) -> tuple[bool, str]:
    if control.status != "ok":
        return False, SOURCE_NOTES_FA.get(control.status, SOURCE_NOTES_FA["error"])
    if not control_ok(control):
        return False, SOURCE_NOTES_FA["control_failed"]
    with_probe = [n.network for n in control.networks if n.had_probe]
    without = [n.network for n in control.networks if not n.had_probe]
    prefix = "دست‌دادن TLS کار می‌کند" if control.kind == "https" else "سنجش کار می‌کند"
    note = f"{prefix}. شبکه‌های دارای نقطه‌ی سنجش: " + ("، ".join(with_probe) or "-")
    if without:
        note += " · بی نقطه‌ی سنجش: " + "، ".join(without)
    return True, note[:200]


async def _store_sources(db: Db, result: CycleResult) -> None:
    for source, tcp_control in result.controls.items():
        # Globalping's health is that of the TLS control (the part that decides) whenever it ran.
        control = result.tls_control if source == "globalping" and result.tls_control else tcp_control
        ok, note = source_health(control)
        values: dict[str, Any] = {"ok": ok, "note": note, "checked_at": result.finished_at}
        if source == "globalping" and result.gp_rate is not None:
            values |= {
                "ratelimit_limit": result.gp_rate.limit,
                "ratelimit_remaining": result.gp_rate.remaining,
                "ratelimit_reset_at": result.gp_rate.reset_at,
            }
        if source == "globalping" and result.tls_probes:
            values["tls_probes"] = result.tls_probes
        stmt = insert(ProbeSource).values(name=source, **values)
        await db.execute(stmt.on_conflict_do_update(index_elements=[ProbeSource.name], set_=values))


async def globalping_rate(db: Db) -> RateLimit | None:
    r = (
        await db.execute(
            select(
                ProbeSource.ratelimit_limit, ProbeSource.ratelimit_remaining, ProbeSource.ratelimit_reset_at
            ).where(ProbeSource.name == "globalping")
        )
    ).first()
    if r is None or r.ratelimit_remaining is None or r.ratelimit_reset_at is None:
        return None
    return RateLimit(
        limit=int(r.ratelimit_limit or 0), remaining=int(r.ratelimit_remaining), reset_at=r.ratelimit_reset_at
    )


async def last_tls_probes(db: Db) -> int | None:
    """How many probes the last TLS control used (None before the first one)."""
    value = (
        await db.execute(select(ProbeSource.tls_probes).where(ProbeSource.name == "globalping"))
    ).scalar_one_or_none()
    return int(value) if value else None


async def record_globalping_rate(db: Db, rate: RateLimit) -> None:
    """Remember Globalping's allowance after a measurement made outside a probe cycle (the domain
    filter checks, the target candidates). Only the rate columns of an existing row change: the
    source's health stays what the last probe cycle saw, and no row means no cycle has run yet."""
    await db.execute(
        update(ProbeSource)
        .where(ProbeSource.name == "globalping")
        .values(
            ratelimit_limit=rate.limit,
            ratelimit_remaining=rate.remaining,
            ratelimit_reset_at=rate.reset_at,
        )
    )


async def ready_node_count(db: Db) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(Node).where(Node.state == NodeState.READY.value)
            )
        ).scalar_one()
    )


async def prune(db: Db, settings: ProbeSettings, now: datetime) -> int:
    result = await db.execute(
        delete(ProbeCycle)
        .where(ProbeCycle.started_at < now - timedelta(days=settings.retention_days))
        .returning(ProbeCycle.id)
    )
    await db.execute(
        delete(ProbeLocationReach).where(
            ProbeLocationReach.at < now - timedelta(days=settings.retention_days)
        )
    )
    return len(result.all())


# --- owner actions and the summary -------------------------------------------------------------


async def request_run(session: AsyncSession, node_id: uuid.UUID, *, actor: str, now: datetime) -> None:
    node = (await session.execute(select(Node.name, Node.ipv4, Node.state).where(Node.id == node_id))).first()
    if node is None or node.state == NodeState.DELETED.value:
        raise ProbeServiceError("not_found")
    if _ipv4(node.ipv4) is None:
        raise ProbeServiceError("node_has_no_ip")
    stmt = insert(ProbeNodeState).values(
        node_id=node_id, run_requested_at=now, verdict="unknown", verdict_reasons=[]
    )
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[ProbeNodeState.node_id], set_={"run_requested_at": now})
    )
    await log_action(
        session,
        actor=actor,
        action="probe_run_requested",
        target=node.name,
        dry_run=False,
        allowed=True,
        context={"node_id": str(node_id)},
        result="a probe round runs within about a minute",
    )
    await session.commit()


async def _latest_networks(db: Db, cycle_id: int, node_id: uuid.UUID, kind: str) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(
                ProbeMeasurement.source,
                ProbeMeasurement.at,
                ProbeMeasurement.port,
                ProbeMeasurement.target,
                ProbeNetworkResult.network,
                ProbeNetworkResult.asn,
                ProbeNetworkResult.ok,
                ProbeNetworkResult.fail,
                ProbeNetworkResult.nodata,
                ProbeNetworkResult.detail,
            )
            .join(ProbeNetworkResult, ProbeNetworkResult.measurement_id == ProbeMeasurement.id)
            .where(
                ProbeMeasurement.cycle_id == cycle_id,
                ProbeMeasurement.node_id == node_id,
                ProbeMeasurement.kind == kind,
            )
            .order_by(ProbeMeasurement.source.desc(), ProbeMeasurement.port, ProbeNetworkResult.network)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {
            "network": r.network,
            "asn": int(r.asn) if r.asn is not None else None,
            "ok": int(r.ok),
            "fail": int(r.fail),
            "at": iso(r.at),
            "source": r.source,
            "probe": int(r.ok) + int(r.fail) + int(r.nodata) > 0,
        }
        if kind == "https":
            detail = r.detail if isinstance(r.detail, dict) else {}
            item |= {
                "kind": "https",
                "port": r.port,
                "failure": detail.get("failure") if int(r.fail) else None,
            }
        out.append(item)
    return out


async def summary(db: Db) -> dict[str, Any]:
    last_run_at = (await db.execute(select(func.max(ProbeCycle.finished_at)))).scalar_one_or_none()
    sources_rows = {
        r.name: r
        for r in (await db.execute(select(ProbeSource.name, ProbeSource.ok, ProbeSource.note))).all()
    }
    sources = [
        {
            "name": name,
            "ok": bool(sources_rows[name].ok) if name in sources_rows else False,
            "note": sources_rows[name].note if name in sources_rows else "هنوز سنجشی انجام نشده است.",
            # False until the source was used once: "not measured yet", not "unreachable".
            "checked": name in sources_rows,
        }
        for name in SOURCES
    ]
    nodes = (
        await db.execute(
            select(
                Node.id,
                Node.name,
                Node.ipv4,
                Node.location,
                ProbeNodeState.verdict,
                ProbeNodeState.verdict_at,
                ProbeNodeState.verdict_reasons,
            )
            .outerjoin(ProbeNodeState, ProbeNodeState.node_id == Node.id)
            .where(
                Node.state != NodeState.DELETED.value,
                (Node.state == NodeState.READY.value) | ProbeNodeState.node_id.is_not(None),
            )
            .order_by(Node.name)
        )
    ).all()
    targets: list[dict[str, Any]] = []
    for n in nodes:
        latest_cycle = (
            await db.execute(
                select(ProbeRound.cycle_id)
                .where(ProbeRound.node_id == n.id)
                .order_by(ProbeRound.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        networks: list[dict[str, Any]] = []
        tcp_networks: list[dict[str, Any]] = []
        tls_target: str | None = None
        if latest_cycle is not None:
            networks = await _latest_networks(db, latest_cycle, n.id, "https")
            tcp_networks = await _latest_networks(db, latest_cycle, n.id, "tcp")
            tls_target = (
                await db.execute(
                    select(ProbeMeasurement.target)
                    .where(
                        ProbeMeasurement.cycle_id == latest_cycle,
                        ProbeMeasurement.node_id == n.id,
                        ProbeMeasurement.kind == "https",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        targets.append(
            {
                "node_id": str(n.id),
                "name": n.name,
                "ipv4": _ipv4(n.ipv4),
                "location": n.location,
                "verdict": n.verdict or "unknown",
                "verdict_at": iso(n.verdict_at),
                "verdict_reasons": [str(x) for x in (n.verdict_reasons or [])],
                # TLS handshakes per network and port: what decides (P15).
                "networks": networks,
                "tls_target": tls_target,
                # TCP pings: information only (MCI and Rightel answer a SYN for blocked addresses).
                "tcp_networks": tcp_networks,
            }
        )
    return {"last_run_at": iso(last_run_at), "sources": sources, "targets": targets}

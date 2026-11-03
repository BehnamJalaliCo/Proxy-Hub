"""One probe cycle (network I/O), its storage with verdicts, and the dashboard summary.

A cycle: the control target is measured once per source, then every due node's MTProto port
(Globalping TCP reusing the control's probes, Globalping ICMP for context, check-host TCP on the
same Iranian nodes as the control) and, when the node holds a slot on the active domain, a DNS
check of its name. Results are stored per measurement and per network; the verdict of each node
is recomputed from its stored rounds (proxyhub/probes/verdict.py) and only RECORDED. A changed
verdict is written to actions_log; nothing is replaced automatically in this round.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models import Node
from proxyhub.db.models_agent import TelemtNodeConfig
from proxyhub.db.models_net import (
    ProbeCycle,
    ProbeMeasurement,
    ProbeNetworkResult,
    ProbeNodeState,
    ProbeRound,
    ProbeSource,
)
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.probes.checkhost import CheckHostClient, CheckHostNode
from proxyhub.probes.globalping import GlobalpingClient, RateLimit, dns_body, icmp_body, tcp_body
from proxyhub.probes.settings import ProbeSettings
from proxyhub.probes.types import Measurement, MeasurementStatus
from proxyhub.probes.verdict import SOURCES, RoundObs, SourceObservation, control_ok, decide, observe

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
GpFactory = Callable[[], GlobalpingClient]
ChFactory = Callable[[], CheckHostClient]

ACTOR = "probes"
GP_CONCURRENCY = 4
CH_CONCURRENCY = 2


class ProbeServiceError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Target:
    node_id: uuid.UUID
    name: str
    ip: str
    port: int
    dns_name: str | None
    trigger: str  # 'schedule' | 'manual'


@dataclass
class CycleResult:
    started_at: datetime
    finished_at: datetime
    controls: dict[str, Measurement]
    nodes: list[Measurement] = field(default_factory=list)
    gp_rate: RateLimit | None = None


# --- choosing what to probe ---------------------------------------------------------------------


def _ipv4(value: Any) -> str | None:
    try:
        ip = ipaddress.ip_interface(str(value)).ip
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


async def due_targets(db: Db, settings: ProbeSettings, now: datetime, dns_zone: str | None) -> list[Target]:
    """READY nodes whose last round is older than the interval, and nodes the owner asked for."""
    cutoff = now - timedelta(seconds=settings.interval_s)
    rows = (
        await db.execute(
            select(
                Node.id,
                Node.name,
                Node.ipv4,
                Node.slot,
                Node.state,
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
    targets: list[Target] = []
    for r in rows:
        ip = _ipv4(r.ipv4)
        if ip is None:
            continue
        port = int(r.ports[0]) if r.ports else settings.default_port
        dns_name = f"slot{r.slot}.{dns_zone}" if dns_zone and r.slot and r.slot > 0 else None
        trigger = "manual" if r.run_requested_at is not None else "schedule"
        targets.append(Target(r.id, r.name, ip, port, dns_name, trigger))
    return targets


def globalping_plan(
    settings: ProbeSettings, targets: Sequence[Target], rate: RateLimit | None, now: datetime
) -> tuple[bool, bool, bool]:
    """(run TCP at all, run ICMP, run DNS) so the cycle stays inside Globalping's hourly limit.
    Each measurement costs at most one test per requested network."""
    per = len(settings.globalping_asns)
    core = per * (1 + len(targets))
    icmp = per * len(targets) if settings.icmp else 0
    dns = per * sum(1 for t in targets if t.dns_name) if settings.dns else 0
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


async def run_cycle(
    targets: Sequence[Target],
    settings: ProbeSettings,
    *,
    gp_factory: GpFactory = GlobalpingClient,
    ch_factory: ChFactory = CheckHostClient,
    gp_rate: RateLimit | None = None,
) -> CycleResult:
    started = datetime.now(UTC)
    asns = list(settings.globalping_asns)
    run_gp, run_icmp, run_dns = globalping_plan(settings, targets, gp_rate, started)
    gp_sem, ch_sem = asyncio.Semaphore(GP_CONCURRENCY), asyncio.Semaphore(CH_CONCURRENCY)
    host, cport = settings.control_host, settings.control_port
    async with gp_factory() as gp, ch_factory() as ch:

        async def gp_control() -> tuple[Measurement, str | None]:
            if not run_gp:
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
            if not settings.checkhost:
                return Measurement("check-host", "tcp", "control", host, cport, "skipped", started), []
            found = await ch.iran_nodes()
            if found is None:
                return Measurement("check-host", "tcp", "control", host, cport, "error", started), []
            nodes = found[: settings.checkhost_max_nodes]
            return await ch.tcp(host, cport, nodes, role="control"), nodes

        (gp_ctrl, gp_ctrl_id), (ch_ctrl, ch_nodes) = await asyncio.gather(gp_control(), ch_control())
        # Reuse the control's exact probes; fall back to the ASN list only if the control itself
        # failed for a transient reason (so the node is still measured).
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
        for t in targets:
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
        rate = gp.last_rate or gp_rate
    return CycleResult(
        started_at=started,
        finished_at=datetime.now(UTC),
        controls={"globalping": gp_ctrl, "check-host": ch_ctrl},
        nodes=node_results,
        gp_rate=rate,
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
        expected_ip=t.ip if kind == "dns" else None,
        node_id=t.node_id,
    )
    return measurement


# --- storage and verdicts -----------------------------------------------------------------------


async def _store_measurement(db: Db, cycle_id: int, m: Measurement) -> int:
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


async def store_cycle(
    db: Db, result: CycleResult, targets: Sequence[Target], settings: ProbeSettings
) -> dict[uuid.UUID, tuple[str, list[str]]]:
    """Store everything, compute each node's verdict, record source health. Returns verdicts."""
    cycle_id = (
        await db.execute(
            insert(ProbeCycle)
            .values(started_at=result.started_at, finished_at=result.finished_at)
            .returning(ProbeCycle.id)
        )
    ).scalar_one()
    for control in result.controls.values():
        await _store_measurement(db, cycle_id, control)
    by_node: dict[uuid.UUID, dict[str, Measurement]] = {}
    for m in result.nodes:
        await _store_measurement(db, cycle_id, m)
        if m.kind == "tcp" and m.node_id is not None:
            by_node.setdefault(m.node_id, {})[m.source] = m

    now = result.finished_at
    max_age = timedelta(seconds=settings.max_round_age_s)
    verdicts: dict[uuid.UUID, tuple[str, list[str]]] = {}
    for t in targets:
        node_tcp = by_node.get(t.node_id, {})
        observations = {s: observe(node_tcp.get(s), result.controls.get(s)) for s in SOURCES}
        previous = await _previous_rounds(db, t.node_id, now - max_age, settings.min_rounds - 1)
        current = RoundObs(at=result.started_at, observations=observations)
        verdict, reasons = decide(
            [current, *previous], now=now, min_rounds=settings.min_rounds, max_age=max_age
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
                },
                result=f"reachability from Iran: {old or 'unknown'} -> {verdict} (recorded only)",
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
    note = "سنجش کار می‌کند. شبکه‌های دارای نقطه‌ی سنجش: " + ("، ".join(with_probe) or "-")
    if without:
        note += " · بی نقطه‌ی سنجش: " + "، ".join(without)
    return True, note[:200]


async def _store_sources(db: Db, result: CycleResult) -> None:
    for source, control in result.controls.items():
        ok, note = source_health(control)
        values: dict[str, Any] = {"ok": ok, "note": note, "checked_at": result.finished_at}
        if source == "globalping" and result.gp_rate is not None:
            values |= {
                "ratelimit_limit": result.gp_rate.limit,
                "ratelimit_remaining": result.gp_rate.remaining,
                "ratelimit_reset_at": result.gp_rate.reset_at,
            }
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


async def prune(db: Db, settings: ProbeSettings, now: datetime) -> int:
    result = await db.execute(
        delete(ProbeCycle)
        .where(ProbeCycle.started_at < now - timedelta(days=settings.retention_days))
        .returning(ProbeCycle.id)
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
        if latest_cycle is not None:
            rows = (
                await db.execute(
                    select(
                        ProbeMeasurement.source,
                        ProbeMeasurement.at,
                        ProbeNetworkResult.network,
                        ProbeNetworkResult.asn,
                        ProbeNetworkResult.ok,
                        ProbeNetworkResult.fail,
                        ProbeNetworkResult.nodata,
                    )
                    .join(ProbeNetworkResult, ProbeNetworkResult.measurement_id == ProbeMeasurement.id)
                    .where(
                        ProbeMeasurement.cycle_id == latest_cycle,
                        ProbeMeasurement.node_id == n.id,
                        ProbeMeasurement.kind == "tcp",
                    )
                    .order_by(ProbeMeasurement.source.desc(), ProbeNetworkResult.network)
                )
            ).all()
            networks = [
                {
                    "network": r.network,
                    "asn": int(r.asn) if r.asn is not None else None,
                    "ok": int(r.ok),
                    "fail": int(r.fail),
                    "at": iso(r.at),
                    "source": r.source,
                    "probe": int(r.ok) + int(r.fail) + int(r.nodata) > 0,
                }
                for r in rows
            ]
        targets.append(
            {
                "node_id": str(n.id),
                "name": n.name,
                "ipv4": _ipv4(n.ipv4),
                "verdict": n.verdict or "unknown",
                "verdict_at": iso(n.verdict_at),
                "verdict_reasons": [str(x) for x in (n.verdict_reasons or [])],
                "networks": networks,
            }
        )
    return {"last_run_at": iso(last_run_at), "sources": sources, "targets": targets}

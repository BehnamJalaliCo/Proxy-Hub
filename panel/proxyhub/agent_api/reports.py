"""Agent reports: a strict schema of aggregate counters, stored without any IP (rule 7).

What a node may tell us is closed: per Telemt user (secret) only ``current_connections`` and
``total_octets``; for the node only process and system counters; and, optionally, the aggregate
``usage_agg`` block of decision D39 (proxyhub/analytics/usage.py). Unknown fields are refused at every
level, types are strict (no coercion), every string has a narrow pattern, and the whole decoded
document is scanned for anything shaped like an IPv4/IPv6 address before it is even validated.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.agent_api.validation import (
    ERROR_CODE,
    ETAG,
    VERSION,
    JsonTooDeep,
    find_ip_like,
    valid_user_name,
)
from proxyhub.analytics.usage import UsageAgg, store_usage_agg
from proxyhub.db.models import MtproxyStat
from proxyhub.db.models_agent import AgentNode, AgentNodeReport, TelemtUser
from proxyhub.reality.report_schema import XrayCounters
from proxyhub.reality.store import XrayUserTotal, store_xray_report

MAX_REPORT_USERS = 1100
INT64_MAX = 2**63 - 1

Version = Annotated[str, StringConstraints(pattern=f"^{VERSION.pattern}$")]
ErrorCode = Annotated[str, StringConstraints(pattern=f"^{ERROR_CODE.pattern}$")]
Etag = Annotated[str, StringConstraints(pattern=f"^{ETAG.pattern}$")]
Counter = Annotated[int, Field(ge=0, le=INT64_MAX)]
Seconds = Annotated[float, Field(ge=0, le=20 * 365 * 86400)]


class ReportRejected(ValueError):
    """The report broke the schema or carried something IP-like; ``code`` is safe to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class UserCounters(_Strict):
    name: str
    current_connections: Annotated[int, Field(ge=0, le=10_000_000)]
    total_octets: Counter

    @model_validator(mode="after")
    def _name(self) -> UserCounters:
        # The agent's own placeholder user is never reported; any other name must be valid.
        if not valid_user_name(self.name):
            raise ValueError("invalid user name")
        return self


class TelemtCounters(_Strict):
    up: bool
    version: Version | None
    uptime_s: Seconds | None
    connections_total: Counter | None
    connections_bad_total: Counter | None
    handshake_timeouts_total: Counter | None
    users: Annotated[list[UserCounters], Field(max_length=MAX_REPORT_USERS)]

    @model_validator(mode="after")
    def _unique(self) -> TelemtCounters:
        names = [u.name for u in self.users]
        if len(names) != len(set(names)):
            raise ValueError("duplicate user")
        return self


class SystemCounters(_Strict):
    uptime_s: Seconds | None
    load1: Annotated[float, Field(ge=0, le=100_000)] | None
    mem_total_bytes: Counter | None
    mem_available_bytes: Counter | None
    cpu_count: Annotated[int, Field(ge=1, le=4096)] | None


class AgentReport(_Strict):
    format: Literal[1]
    agent_version: Version
    applied_etag: Etag | None
    apply_ok: bool
    apply_error: ErrorCode | None
    telemt: TelemtCounters
    system: SystemCounters
    # Optional (agents before D39 do not send it): proxy-usage counters made on the node from
    # connection IPs IN MEMORY; no IP ever leaves the node (proxyhub/analytics/usage.py).
    usage_agg: UsageAgg | None = None
    # From agent 0.2.0 on (VLESS Reality, proxyhub/reality); older agents send no such key.
    xray: XrayCounters | None = None


def parse_report(data: Any) -> AgentReport:
    """Validate a decoded JSON report. Raises :class:`ReportRejected` (never echoes input)."""
    if not isinstance(data, dict):
        raise ReportRejected("not_an_object")
    try:
        ip_like = find_ip_like(data)
    except JsonTooDeep:
        raise ReportRejected("too_deep") from None
    if ip_like is not None:
        raise ReportRejected("ip_like_value")
    try:
        return AgentReport.model_validate(data)
    except ValueError:  # pydantic.ValidationError is a ValueError
        raise ReportRejected("schema") from None


async def store_report(session: AsyncSession, node_id: uuid.UUID, report: AgentReport, now: datetime) -> int:
    """Store one report. Returns how many per-user rows went to ``mtproxy_stats``. The caller commits."""
    users = {
        u.name: u
        for u in (await session.execute(select(TelemtUser).where(TelemtUser.node_id == node_id)))
        .scalars()
        .all()
    }
    stats_rows = 0
    for counters in report.telemt.users:
        user = users.get(counters.name)
        if user is None:  # removed meanwhile, or never ours: nothing to attach it to
            continue
        user.last_current_connections = counters.current_connections
        user.last_total_octets = counters.total_octets
        user.last_stats_at = now
        if user.registration_id is not None:
            session.add(
                MtproxyStat(
                    registration_id=user.registration_id,
                    at=now,
                    source="node",
                    current_connections=counters.current_connections,
                    total_octets=counters.total_octets,
                    extra={"node_id": str(node_id), "telemt_uptime_s": report.telemt.uptime_s},
                )
            )
            stats_rows += 1
    telemt, system = report.telemt, report.system
    session.add(
        AgentNodeReport(
            node_id=node_id,
            at=now,
            applied_etag=report.applied_etag,
            apply_ok=report.apply_ok,
            apply_error=report.apply_error,
            telemt_up=telemt.up,
            telemt_uptime_s=telemt.uptime_s,
            connections_total=telemt.connections_total,
            connections_bad_total=telemt.connections_bad_total,
            handshake_timeouts_total=telemt.handshake_timeouts_total,
            users_reported=len(telemt.users),
            sys_uptime_s=system.uptime_s,
            load1=system.load1,
            mem_total_bytes=system.mem_total_bytes,
            mem_available_bytes=system.mem_available_bytes,
            cpu_count=system.cpu_count,
        )
    )
    values: dict[str, Any] = {
        "last_report_at": now,
        "last_applied_etag": report.applied_etag,
        "last_apply_ok": report.apply_ok,
        "last_apply_error": report.apply_error,
    }
    if telemt.version is not None:
        values["telemt_version"] = telemt.version
    await session.execute(update(AgentNode).where(AgentNode.node_id == node_id).values(**values))
    if report.usage_agg is not None:
        await store_usage_agg(session, node_id, report.usage_agg, now)
    if report.xray is not None:
        x = report.xray
        await store_xray_report(
            session,
            node_id,
            enabled=x.enabled,
            up=x.up,
            version=x.version,
            uptime_s=x.uptime_s,
            target_ok=x.target_ok,
            checked=x.checked(),
            apply_error=x.apply_error,
            users=[XrayUserTotal(u.email, u.total_octets) for u in x.users],
            now=now,
        )
    return stats_rows

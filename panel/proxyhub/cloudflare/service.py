"""Cloudflare service: token verification and storage, zones and the domain pool, desired slot
records, and applying a DNS plan under the managed-record rule (proxyhub/cloudflare/records.py).

Every write that has to survive a crash between "decided" and "done in Cloudflare" runs in its own
short transaction on the engine (``durable``): the row of a record is committed BEFORE the record
is created, so a crash right after the API call leaves a row whose ``ref`` the next run finds in
the record comment (adopt) instead of a marked record nobody owns. Each Cloudflare change and its
actions_log row are committed together right after the call.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from proxyhub.cloudflare.client import CloudflareClient, CloudflareError, Zone, valid_token_format
from proxyhub.cloudflare.pool import PoolState, check_pool_transition, displaced_state
from proxyhub.cloudflare.records import Change, Desired, OwnedRow, owned_row, plan_zone
from proxyhub.db.models import Node
from proxyhub.db.models_net import CfRecord, CfState, CfZone
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.vault import SecretValue, Vault, secret_exists

log = logging.getLogger(__name__)

TOKEN_SECRET = "cloudflare:token"  # noqa: S105 - the vault entry name, not a secret
PERM_ZONE_READ = "Zone:Read"
PERM_DNS_EDIT = "DNS:Edit"
GONE = "gone"

# Nodes whose IP a slot name keeps pointing at. READY is preferred; the others still hold their
# server and IP, so a short unhealthy spell never turns a working name into NXDOMAIN.
HOLDING_STATES = (
    NodeState.READY,
    NodeState.UNHEALTHY,
    NodeState.REPAIRING,
    NodeState.BURNED,
    NodeState.DRAINING,
)

Db = AsyncConnection | AsyncSession
ClientFactory = Callable[[SecretValue], CloudflareClient]


class CfServiceError(Exception):
    """``code`` is the API contract error code; ``extra`` holds contract fields only."""

    def __init__(self, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra


def _unavailable(exc: CloudflareError) -> CfServiceError:
    log.warning("cloudflare call failed: %s", exc)
    return CfServiceError("cf_unavailable")


# --- token -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenCheck:
    kind: str
    zones: list[Zone]
    dns_edit: dict[str, bool]
    missing: list[str]


async def _verify(cf: CloudflareClient) -> str:
    """'user' or 'account'; raises cf_token_invalid / cf_unavailable."""
    try:
        status = await cf.verify_user_token()
        kind = "user"
    except CloudflareError as exc:
        if exc.kind not in ("denied", "rejected", "not_found"):
            raise _unavailable(exc) from None
        status, kind = await _verify_account_token(cf), "account"
    if status != "active":
        raise CfServiceError("cf_token_invalid")
    return kind


async def _verify_account_token(cf: CloudflareClient) -> str:
    """An account-owned token fails the user endpoint; its zones name the account to verify at."""
    try:
        zones = await cf.list_zones()
        account_ids = sorted({z.account_id for z in zones if z.account_id})
        if not account_ids:
            raise CfServiceError("cf_token_invalid")
        return await cf.verify_account_token(account_ids[0])
    except CloudflareError as exc:
        if exc.kind in ("denied", "rejected", "not_found", "invalid_response"):
            raise CfServiceError("cf_token_invalid") from None
        raise _unavailable(exc) from None


async def check_token(cf: CloudflareClient) -> TokenCheck:
    """Verify the token and confirm Zone:Read and DNS:Edit on every zone it sees, changing nothing."""
    kind = await _verify(cf)
    try:
        zones = await cf.list_zones()
    except CloudflareError as exc:
        if exc.kind == "denied":
            return TokenCheck(kind, [], {}, [PERM_ZONE_READ])
        raise _unavailable(exc) from None
    missing: set[str] = set() if zones else {PERM_ZONE_READ}
    dns_edit: dict[str, bool] = {}
    for zone in zones:
        try:
            await cf.list_dns_records(zone.id, first_page_only=True)
            dns_edit[zone.id] = await cf.can_edit_dns(zone.id)
        except CloudflareError as exc:
            if exc.kind != "denied":
                raise _unavailable(exc) from None
            dns_edit[zone.id] = False
        if not dns_edit[zone.id]:
            missing.add(PERM_DNS_EDIT)
    return TokenCheck(kind, zones, dns_edit, sorted(missing))


@dataclass(frozen=True)
class StateView:
    verified: bool
    checked_at: datetime | None
    error: str | None
    missing: list[str]
    token_kind: str | None
    zones_refreshed_at: datetime | None


@dataclass(frozen=True)
class ZoneView:
    id: str
    name: str
    status: str
    pool_state: str
    dns_edit: bool | None


_ZONE_COLS = (CfZone.id, CfZone.name, CfZone.status, CfZone.pool_state, CfZone.dns_edit)


def _zone_view(r: Any) -> ZoneView:
    return ZoneView(id=r.id, name=r.name, status=r.status, pool_state=r.pool_state, dns_edit=r.dns_edit)


async def load_state(db: Db) -> StateView | None:
    """Explicit columns (not the ORM entity), so this works on a session and on a connection."""
    r = (
        await db.execute(
            select(
                CfState.verified,
                CfState.checked_at,
                CfState.error,
                CfState.missing,
                CfState.token_kind,
                CfState.zones_refreshed_at,
            ).where(CfState.id == 1)
        )
    ).first()
    if r is None:
        return None
    return StateView(
        verified=bool(r.verified),
        checked_at=r.checked_at,
        error=r.error,
        missing=[str(m) for m in (r.missing or [])],
        token_kind=r.token_kind,
        zones_refreshed_at=r.zones_refreshed_at,
    )


async def save_state(db: Db, now: datetime, **values: Any) -> None:
    values = {**values, "updated_at": now}
    stmt = insert(CfState).values(id=1, **values)
    await db.execute(stmt.on_conflict_do_update(index_elements=[CfState.id], set_=values))


async def store_zones(db: Db, zones: Sequence[Zone], dns_edit: dict[str, bool], now: datetime) -> list[str]:
    """Upsert the zones the token lists; zones it no longer lists become 'gone' (pool state kept).
    Returns the names of zones that just became gone."""
    for zone in zones:
        values: dict[str, Any] = {
            "name": zone.name,
            "status": zone.status,
            "seen_at": now,
            "updated_at": now,
            "dns_edit": dns_edit.get(zone.id),
        }
        stmt = insert(CfZone).values(id=zone.id, pool_state=PoolState.NONE.value, **values)
        await db.execute(stmt.on_conflict_do_update(index_elements=[CfZone.id], set_=values))
    listed = [z.id for z in zones]
    gone = (
        await db.execute(
            update(CfZone)
            .where(CfZone.id.not_in(listed) if listed else CfZone.id.is_not(None), CfZone.status != GONE)
            .values(status=GONE, updated_at=now)
            .returning(CfZone.name)
        )
    ).scalars()
    return list(gone)


async def set_token(
    session: AsyncSession,
    vault: Vault,
    token: str,
    *,
    actor: str,
    now: datetime,
    client_factory: ClientFactory = CloudflareClient,
) -> dict[str, Any]:
    """Verify a token for real and store it in the vault only if it has every permission needed.
    Raises :class:`CfServiceError` (nothing stored) otherwise. The token is never logged."""
    token = token.strip()
    if not valid_token_format(token):
        await _log_token_attempt(session, actor, "cf_token_invalid", now)
        raise CfServiceError("cf_token_invalid")
    secret = SecretValue(token)
    try:
        async with client_factory(secret) as cf:
            check = await check_token(cf)
    except CfServiceError as exc:
        await _log_token_attempt(session, actor, exc.code, now)
        raise
    if check.missing:
        await _log_token_attempt(session, actor, "cf_permissions_missing", now, missing=check.missing)
        raise CfServiceError("cf_permissions_missing", missing=check.missing)
    await vault.put(session, TOKEN_SECRET, secret)
    gone = await store_zones(session, check.zones, check.dns_edit, now)
    await save_state(
        session,
        now,
        verified=True,
        checked_at=now,
        error=None,
        missing=[],
        token_kind=check.kind,
        zones_refreshed_at=now,
    )
    await log_action(
        session,
        actor=actor,
        action="cloudflare_token_set",
        target=None,
        dry_run=False,
        allowed=True,
        reasons=["verified"],
        context={"token_kind": check.kind, "zones": [z.name for z in check.zones], "gone": gone},
        result=f"Cloudflare token verified and stored ({len(check.zones)} zones)",
    )
    await session.commit()
    return await status(session)


async def _log_token_attempt(
    session: AsyncSession, actor: str, code: str, now: datetime, missing: list[str] | None = None
) -> None:
    await session.rollback()
    await log_action(
        session,
        actor=actor,
        action="cloudflare_token_set",
        target=None,
        dry_run=False,
        allowed=False,
        reasons=[code],
        context={"missing": missing or []},
        result="Cloudflare token refused; nothing stored",
    )
    await session.commit()


async def refresh_token_and_zones(db: Db, cf: CloudflareClient, now: datetime, *, actor: str) -> None:
    """Periodic re-check of the stored token (new zones are found here, decision D13)."""
    previous = await load_state(db)
    was_verified = bool(previous and previous.verified)
    try:
        check = await check_token(cf)
    except CfServiceError as exc:
        if exc.code == "cf_token_invalid":
            # Checked again in an hour (or at once when the owner stores a new token).
            await save_state(
                db, now, verified=False, checked_at=now, error=exc.code, missing=[], zones_refreshed_at=now
            )
            if was_verified:
                await log_action(
                    db,
                    actor=actor,
                    action="cloudflare_token_check",
                    target=None,
                    dry_run=False,
                    allowed=False,
                    reasons=[exc.code],
                    result="the stored Cloudflare token is no longer valid; DNS changes stopped",
                )
        else:
            await save_state(db, now, error=exc.code)
        return
    gone = await store_zones(db, check.zones, check.dns_edit, now)
    verified = not check.missing
    await save_state(
        db,
        now,
        verified=verified,
        checked_at=now,
        error=None if verified else "cf_permissions_missing",
        missing=check.missing,
        token_kind=check.kind,
        zones_refreshed_at=now,
    )
    if gone or was_verified != verified:
        await log_action(
            db,
            actor=actor,
            action="cloudflare_token_check",
            target=None,
            dry_run=False,
            allowed=verified,
            reasons=["zones_gone"] if gone else (["verified"] if verified else ["cf_permissions_missing"]),
            context={"gone": gone, "missing": check.missing},
            result="Cloudflare token re-checked",
        )


# --- status and pool ---------------------------------------------------------------------------


async def status(db: Db) -> dict[str, Any]:
    token_set = await secret_exists(db, TOKEN_SECRET)
    state = await load_state(db)
    zones = [_zone_view(r) for r in (await db.execute(select(*_ZONE_COLS).order_by(CfZone.name))).all()]
    records = (
        await db.execute(
            select(
                CfRecord.name,
                CfRecord.type,
                CfRecord.content,
                CfRecord.ttl,
                CfRecord.proxied,
                CfRecord.updated_at,
            )
            .where(CfRecord.cf_record_id.is_not(None))
            .order_by(CfRecord.name, CfRecord.type)
        )
    ).all()
    return {
        "token_set": token_set,
        "verified": bool(token_set and state is not None and state.verified),
        "checked_at": iso(state.checked_at) if state is not None else None,
        "error": state.error if state is not None else None,
        "zones": [
            {
                "id": z.id,
                "name": z.name,
                "status": z.status,
                "pool_state": z.pool_state,
                "dns_edit": z.dns_edit,
            }
            for z in zones
        ],
        "records": [
            {
                "name": r.name,
                "type": r.type,
                "content": r.content,
                "ttl": r.ttl,
                "proxied": r.proxied,
                "managed": True,
                "updated_at": iso(r.updated_at),
            }
            for r in records
        ],
    }


async def set_pool_state(
    session: AsyncSession, zone_id: str, target: PoolState, *, actor: str, now: datetime
) -> dict[str, Any]:
    """Owner action. Raises CfServiceError(not_found | pool_transition_invalid | zone_not_ready)."""
    # Lock every zone row: two concurrent activations must not both pass (the partial unique index
    # would also refuse the second one).
    zones = {z.id: z for z in (await session.execute(select(CfZone).with_for_update())).scalars().all()}
    zone = zones.get(zone_id)
    if zone is None:
        await session.rollback()
        raise CfServiceError("not_found")
    current = PoolState(zone.pool_state)
    try:
        changed = check_pool_transition(current, target, zone_status=zone.status, dns_edit=zone.dns_edit)
    except ValueError as exc:
        await session.rollback()
        raise CfServiceError(getattr(exc, "code", "pool_transition_invalid")) from None
    if not changed:
        await session.rollback()
        return await status(session)
    if target == PoolState.ACTIVE:
        for other in zones.values():
            if other.id != zone.id and other.pool_state == PoolState.ACTIVE.value:
                new_state = displaced_state(PoolState.ACTIVE)
                other.pool_state = new_state.value
                other.pool_changed_at = now
                other.updated_at = now
                await session.flush()
                await _log_pool(session, actor, other.name, PoolState.ACTIVE, new_state, "replaced")
    zone.pool_state = target.value
    zone.pool_changed_at = now
    zone.updated_at = now
    await session.flush()
    await _log_pool(session, actor, zone.name, current, target, "owner")
    await session.commit()
    return await status(session)


async def _log_pool(db: Db, actor: str, name: str, old: PoolState, new: PoolState, why: str) -> None:
    await log_action(
        db,
        actor=actor,
        action="cloudflare_pool_change",
        target=name,
        dry_run=False,
        allowed=True,
        reasons=[why],
        context={"from": old.value, "to": new.value},
        result=f"{name}: {old.value} -> {new.value}",
    )


# --- desired records ---------------------------------------------------------------------------


def slot_name(slot: int, zone_name: str) -> str:
    return f"slot{slot}.{zone_name}"


async def desired_slot_records(db: Db, zone_name: str) -> list[Desired]:
    """slot<N>.<zone> -> the node holding slot N: a READY node first (newest ready_at), else the
    newest node that still holds its server (HOLDING_STATES)."""
    rows = (
        await db.execute(
            select(Node.id, Node.slot, Node.state, Node.ipv4, Node.ready_at, Node.created_at).where(
                Node.slot.is_not(None),
                Node.ipv4.is_not(None),
                Node.state.in_([s.value for s in HOLDING_STATES]),
            )
        )
    ).all()
    best: dict[int, tuple[tuple[int, float, float], uuid.UUID, str]] = {}
    for r in rows:
        try:
            ip = ipaddress.ip_interface(str(r.ipv4)).ip
        except ValueError:
            continue
        if ip.version != 4 or r.slot is None or r.slot < 1:
            continue
        rank = (
            1 if r.state == NodeState.READY.value else 0,
            r.ready_at.timestamp() if r.ready_at else 0.0,
            r.created_at.timestamp(),
        )
        current = best.get(int(r.slot))
        if current is None or rank > current[0]:
            best[int(r.slot)] = (rank, r.id, str(ip))
    return [
        Desired(name=slot_name(slot, zone_name), type="A", content=ip, node_id=node_id, purpose="slot")
        for slot, (_, node_id, ip) in sorted(best.items())
    ]


async def active_zone(db: Db) -> ZoneView | None:
    """The active domain, if Cloudflare still lists it."""
    r = (
        await db.execute(
            select(*_ZONE_COLS).where(CfZone.pool_state == PoolState.ACTIVE.value, CfZone.status != GONE)
        )
    ).first()
    return _zone_view(r) if r is not None else None


async def owned_rows(db: Db, zone_id: str, purpose: str) -> list[OwnedRow]:
    rows = (
        await db.execute(
            select(
                CfRecord.id,
                CfRecord.zone_id,
                CfRecord.cf_record_id,
                CfRecord.name,
                CfRecord.type,
                CfRecord.content,
                CfRecord.ttl,
                CfRecord.proxied,
            ).where(CfRecord.zone_id == zone_id, CfRecord.purpose == purpose)
        )
    ).all()
    return [
        OwnedRow(
            ref=r.id,
            zone_id=r.zone_id,
            cf_record_id=r.cf_record_id,
            name=r.name,
            type=r.type,
            content=r.content,
            ttl=r.ttl,
            proxied=r.proxied,
        )
        for r in rows
    ]


async def zones_with_rows(db: Db, purpose: str) -> list[str]:
    return list(
        (await db.execute(select(CfRecord.zone_id).where(CfRecord.purpose == purpose).distinct())).scalars()
    )


# --- applying a plan -----------------------------------------------------------------------------


@dataclass
class ApplyReport:
    api_calls: int = 0
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    conflicts: list[tuple[str, str, str]] = field(default_factory=list)
    stopped: bool = False


@asynccontextmanager
async def _durable(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.begin() as conn:
        yield conn


async def _log_dns(
    db: Db, actor: str, action: str, change: Change, *, allowed: bool, result: str, **context: Any
) -> None:
    await log_action(
        db,
        actor=actor,
        action=action,
        target=f"{change.name} {change.type}",
        dry_run=False,
        allowed=allowed,
        reasons=[change.op],
        context={"zone_id": change.zone_id, **context},
        result=result,
    )


async def reconcile_zone(
    engine: AsyncEngine,
    cf: CloudflareClient,
    zone_id: str,
    desired: Sequence[Desired],
    *,
    purpose: str,
    actor: str,
    max_api_calls: int,
    known_conflicts: set[tuple[str, str, str]] | None = None,
) -> ApplyReport:
    """List the zone, plan against our rows of ``purpose``, apply. Foreign records are only read."""
    async with engine.connect() as conn:
        rows = await owned_rows(conn, zone_id, purpose)
    remote = await cf.list_dns_records(zone_id)
    changes = plan_zone(zone_id, desired, remote, rows)
    return await apply_plan(
        engine,
        cf,
        changes,
        rows,
        purpose=purpose,
        actor=actor,
        max_api_calls=max_api_calls,
        known_conflicts=known_conflicts if known_conflicts is not None else set(),
    )


async def apply_plan(
    engine: AsyncEngine,
    cf: CloudflareClient,
    changes: Iterable[Change],
    rows: Sequence[OwnedRow],
    *,
    purpose: str,
    actor: str,
    max_api_calls: int,
    known_conflicts: set[tuple[str, str, str]],
) -> ApplyReport:
    report = ApplyReport()
    for change in changes:
        if change.op == "conflict":
            key = (change.zone_id, change.name, change.type)
            report.conflicts.append(key)
            if key not in known_conflicts:
                known_conflicts.add(key)
                async with _durable(engine) as db:
                    await _log_dns(
                        db,
                        actor,
                        "dns_conflict",
                        change,
                        allowed=False,
                        result="a record we do not manage already uses this name; left untouched",
                    )
            continue
        if change.op in ("adopt", "sync", "forget"):
            await _apply_local(engine, change, actor)
            report.done.append(f"{change.op}:{change.name}")
            continue
        if report.api_calls >= max_api_calls or report.stopped:
            continue
        report.api_calls += 1
        try:
            await _apply_remote(engine, cf, change, rows, purpose=purpose, actor=actor)
            report.done.append(f"{change.op}:{change.name}")
        except CloudflareError as exc:
            report.failed.append(f"{change.op}:{change.name}")
            async with _durable(engine) as db:
                await _log_dns(
                    db, actor, f"dns_{change.op}", change, allowed=True, result=f"failed: {exc.kind}"
                )
            if exc.kind in ("rate_limited", "unavailable", "denied"):
                report.stopped = True
    return report


async def _apply_local(engine: AsyncEngine, change: Change, actor: str) -> None:
    row, remote = change.row, change.remote
    assert row is not None
    async with _durable(engine) as db:
        if change.op == "forget":
            await db.execute(delete(CfRecord).where(CfRecord.id == row.ref))
            await _log_dns(
                db,
                actor,
                "dns_forget",
                change,
                allowed=True,
                result="our record is no longer in Cloudflare; row dropped",
            )
            return
        assert remote is not None
        await db.execute(
            update(CfRecord)
            .where(CfRecord.id == row.ref)
            .values(
                cf_record_id=remote.id,
                content=remote.content,
                ttl=remote.ttl,
                proxied=remote.proxied,
                updated_at=datetime.now(UTC),
            )
        )
        await _log_dns(
            db,
            actor,
            f"dns_{change.op}",
            change,
            allowed=True,
            result="row matched to the record in Cloudflare",
            cf_record_id=remote.id,
        )


async def _apply_remote(
    engine: AsyncEngine,
    cf: CloudflareClient,
    change: Change,
    rows: Sequence[OwnedRow],
    *,
    purpose: str,
    actor: str,
) -> None:
    now = datetime.now(UTC)
    if change.op == "create":
        want = change.desired
        assert want is not None
        ref = change.row.ref if change.row is not None else uuid.uuid4()
        if change.row is None:
            # Committed before the API call: after a crash the record is found by its ref.
            async with _durable(engine) as db:
                await db.execute(
                    insert(CfRecord).values(
                        id=ref,
                        zone_id=change.zone_id,
                        cf_record_id=None,
                        name=want.name.lower(),
                        type=want.type.upper(),
                        content=want.content,
                        ttl=want.ttl,
                        proxied=want.proxied,
                        purpose=purpose,
                        node_id=want.node_id,
                    )
                )
        created = await cf.create_record(change.zone_id, want.spec(ref))
        async with _durable(engine) as db:
            await db.execute(
                update(CfRecord)
                .where(CfRecord.id == ref)
                .values(
                    cf_record_id=created.id,
                    content=created.content,
                    ttl=created.ttl,
                    proxied=created.proxied,
                    node_id=want.node_id,
                    updated_at=now,
                )
            )
            await _log_dns(
                db, actor, "dns_create", change, allowed=True, result="created", content=want.content
            )
        return

    remote = change.remote
    assert remote is not None
    row = owned_row(remote, rows)  # the managed-record rule, checked again right before the call
    if row is None:
        async with _durable(engine) as db:
            await _log_dns(
                db,
                actor,
                f"dns_{change.op}",
                change,
                allowed=False,
                result="refused: not a record this panel manages",
            )
        return
    if change.op == "update":
        want = change.desired
        assert want is not None
        updated = await cf.update_record(change.zone_id, remote.id, want.spec(row.ref))
        async with _durable(engine) as db:
            await db.execute(
                update(CfRecord)
                .where(CfRecord.id == row.ref)
                .values(
                    cf_record_id=updated.id,
                    content=updated.content,
                    ttl=updated.ttl,
                    proxied=updated.proxied,
                    node_id=want.node_id,
                    updated_at=now,
                )
            )
            await _log_dns(
                db,
                actor,
                "dns_update",
                change,
                allowed=True,
                result="updated",
                old_content=remote.content,
                content=want.content,
            )
        return
    if change.op == "delete":
        await cf.delete_record(change.zone_id, remote.id)
        async with _durable(engine) as db:
            await db.execute(delete(CfRecord).where(CfRecord.id == row.ref))
            await _log_dns(
                db, actor, "dns_delete", change, allowed=True, result="deleted", old_content=remote.content
            )
        return
    raise ValueError(f"unknown change {change.op}")

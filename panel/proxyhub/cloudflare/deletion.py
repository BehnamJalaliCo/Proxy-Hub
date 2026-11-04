"""Deleting a filtered domain from Cloudflare (decision D35). This module is the ONLY caller of
:meth:`CloudflareClient.delete_zone` (pinned by tests/test_architecture.py).

A zone is deleted only when ALL of these hold, re-checked inside the deciding transaction with
every zone row locked (:func:`deletion_blockers` names the ones that do not):

1. ``pool_state`` is ``filtered``;
2. ``filtered_at`` is at least :data:`DELETE_GRACE` ago. Until then the owner can move it back to
   ``standby`` from the dashboard, which cancels the deletion;
3. no ``cf_records`` row of ours remains on the zone (the DNS loop moved the slot names away; with
   no active domain to move them to, they stay and the zone is kept);
4. the zone is in ``cf_zones`` and the last refresh still listed it (our token sees it), and it is
   not the active zone;
5. the stored Cloudflare token is verified;
6. the kill switch is OFF (a stored value that does not validate counts as ON);
7. Cloudflare has not refused it for good, no backoff after a transient error is running, and no
   other attempt is in flight.

The deciding transaction commits a claim (``delete_claimed_at``) BEFORE the API call. The owner's
pool changes are refused while it stands (409 ``zone_delete_in_progress``), so a zone moved back to
standby can never be deleted by an attempt decided a moment earlier. The call itself runs with no
transaction open; a claim left by a crash expires after :data:`CLAIM_TTL`.

Outcomes: success (or 404, the zone is already gone) -> ``pool_state = deleted`` and
``deleted_at``. 401/403 -> ``delete_blocked = zone_edit_denied`` (the token lacks Zone · Zone · Edit,
which is optional when a token is stored, because it cannot be proven without deleting something).
Another 4xx refusal (for example a domain registered at Cloudflare Registrar) ->
``delete_blocked = cf_refused:<codes>``. Both are logged and told to the owner once and not tried
again until a token is stored again. Anything else (5xx, rate limit, network) is retried later
with backoff (:func:`retry_delay`).
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.cloudflare import TOKEN_SECRET
from proxyhub.cloudflare.client import CloudflareClient, CloudflareError
from proxyhub.cloudflare.pool import PoolState
from proxyhub.cloudflare.rotation import ACTOR, DELETE_GRACE, GONE, Db, PoolZone, load_zones
from proxyhub.db.models_net import CfRecord, CfState, CfZone
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.telegram.store import enqueue_owner_alert
from proxyhub.vault import secret_exists

log = logging.getLogger(__name__)

P = PoolState

CLAIM_TTL: Final = timedelta(minutes=5)
RETRY_BASE: Final = timedelta(minutes=15)
RETRY_MAX: Final = timedelta(hours=12)
ZONE_EDIT_DENIED: Final = "zone_edit_denied"
CF_REFUSED: Final = "cf_refused"


def retry_delay(attempts: int) -> timedelta:
    """15 min, 30 min, 1 h, ... at most 12 h after the n-th failed attempt."""
    factor: int = 2 ** min(max(attempts - 1, 0), 10)
    return min(RETRY_BASE * factor, RETRY_MAX)


def delete_after(z: PoolZone) -> datetime | None:
    """When a filtered zone becomes deletable (the end of the grace period)."""
    if z.pool_state != P.FILTERED.value or z.filtered_at is None:
        return None
    return z.filtered_at + DELETE_GRACE


@dataclass(frozen=True)
class DeletionContext:
    records_left: int
    token_verified: bool
    kill_switch: bool
    active_zone_id: str | None


def deletion_blockers(z: PoolZone, ctx: DeletionContext, now: datetime) -> list[str]:
    """Every reason this zone must not be deleted now; empty means every condition holds."""
    out: list[str] = []
    if z.pool_state != P.FILTERED.value:
        out.append("not_filtered")
    if z.deleted_at is not None:
        out.append("already_deleted")
    if z.filtered_at is None or now < z.filtered_at + DELETE_GRACE:
        out.append("grace_period")
    if ctx.records_left > 0:
        out.append("records_remaining")
    if z.status == GONE:
        out.append("zone_gone")
    if ctx.active_zone_id == z.id:
        out.append("zone_active")
    if not ctx.token_verified:
        out.append("token_not_verified")
    if ctx.kill_switch:
        out.append("kill_switch_on")
    if z.delete_blocked:
        out.append("delete_blocked")
    if z.delete_retry_at is not None and now < z.delete_retry_at:
        out.append("retry_later")
    if z.delete_claimed_at is not None and now - z.delete_claimed_at < CLAIM_TTL:
        out.append("delete_in_progress")
    return out


async def kill_switch_on(db: Db) -> bool:
    """The fleet's kill switch, read the same way the fleet code reads it; unreadable means ON."""
    try:
        return (await load_fleet_settings(db)).kill_switch
    except SettingsCorrupt:
        return True


async def token_verified(db: Db) -> bool:
    verified = (await db.execute(select(CfState.verified).where(CfState.id == 1))).scalar_one_or_none()
    return bool(verified) and await secret_exists(db, TOKEN_SECRET)


async def deletion_context(db: Db, zone_id: str) -> DeletionContext:
    records_left = (
        await db.execute(select(func.count()).select_from(CfRecord).where(CfRecord.zone_id == zone_id))
    ).scalar_one()
    active = (
        await db.execute(select(CfZone.id).where(CfZone.pool_state == P.ACTIVE.value))
    ).scalar_one_or_none()
    return DeletionContext(
        records_left=int(records_left),
        token_verified=await token_verified(db),
        kill_switch=await kill_switch_on(db),
        active_zone_id=active,
    )


async def allow_deletion_retries(db: Db) -> int:
    """A token was stored again (perhaps now with Zone Edit): deletions Cloudflare refused, and
    backoffs after errors, start over. Returns how many zones were affected."""
    result = await db.execute(
        update(CfZone)
        .where(CfZone.delete_blocked.is_not(None) | CfZone.delete_retry_at.is_not(None))
        .values(delete_blocked=None, delete_error=None, delete_attempts=0, delete_retry_at=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def deletable_zones(db: Db, now: datetime) -> list[str]:
    """Ids of the zones every condition allows to delete now (re-checked again before the call)."""
    out: list[str] = []
    for z in await load_zones(db):
        if z.pool_state != P.FILTERED.value:
            continue
        if not deletion_blockers(z, await deletion_context(db, z.id), now):
            out.append(z.id)
    return out


def classify_delete_error(error: CloudflareError | None) -> tuple[str, str | None]:
    """(outcome, value for ``delete_blocked``) of one ``DELETE /zones/{id}`` call."""
    if error is None:
        return "deleted", None
    if error.kind == "not_found":
        return "already_absent", None
    if error.kind == "denied":
        return ZONE_EDIT_DENIED, ZONE_EDIT_DENIED
    if error.kind == "rejected":
        codes = ",".join(str(c) for c in error.codes[:5])
        return CF_REFUSED, (f"{CF_REFUSED}:{codes}" if codes else CF_REFUSED)
    return "retry_later", None


async def delete_filtered_zone(engine: AsyncEngine, cf: CloudflareClient, zone_id: str, now: datetime) -> str:
    """Decide (locked, re-checked), claim, call Cloudflare, record. Returns the outcome code:
    ``blocked`` (a condition failed; nothing called), ``deleted``, ``already_absent``,
    ``zone_edit_denied``, ``cf_refused`` or ``retry_later``."""
    async with engine.begin() as db:
        zones = {z.id: z for z in await load_zones(db, lock=True)}
        zone = zones.get(zone_id)
        if zone is None or deletion_blockers(zone, await deletion_context(db, zone_id), now):
            return "blocked"
        await db.execute(update(CfZone).where(CfZone.id == zone_id).values(delete_claimed_at=now))
    error: CloudflareError | None = None
    try:
        await cf.delete_zone(zone_id)
    except CloudflareError as exc:
        error = exc
    async with engine.begin() as db:
        return await record_delete_result(db, zone, now, error)


async def record_delete_result(db: Db, zone: PoolZone, now: datetime, error: CloudflareError | None) -> str:
    """Write what one deletion call did (in the caller's transaction) and tell the owner."""
    outcome, blocked = classify_delete_error(error)
    if outcome in ("deleted", "already_absent"):
        return await _deleted(db, zone, now, already=outcome == "already_absent")
    if blocked is not None and error is not None:
        return await _blocked(db, zone, now, blocked, error)
    assert error is not None
    return await _retry(db, zone, now, error)


async def _deleted(db: Db, zone: PoolZone, now: datetime, *, already: bool) -> str:
    await db.execute(
        update(CfZone)
        .where(CfZone.id == zone.id, CfZone.pool_state == P.FILTERED.value)
        .values(
            pool_state=P.DELETED.value,
            pool_changed_at=now,
            deleted_at=now,
            delete_claimed_at=None,
            delete_error=None,
            delete_retry_at=None,
            updated_at=now,
        )
    )
    outcome = "already_absent" if already else "deleted"
    await log_action(
        db,
        actor=ACTOR,
        action="cloudflare_zone_delete",
        target=zone.name,
        dry_run=False,
        allowed=True,
        reasons=[outcome],
        context={"zone_id": zone.id, "filtered_at": iso(zone.filtered_at)},
        result=(
            "filtered domain deleted from Cloudflare"
            if not already
            else "Cloudflare no longer has this zone; marked deleted"
        ),
    )
    if not already:  # a zone someone else removed already needs no news
        name = html.escape(zone.name)
        await enqueue_owner_alert(db, f"🗑 دامنه‌ی فیلترشده‌ی <b>{name}</b> از Cloudflare پاک شد.")
    log.info("domains: zone %s deleted from Cloudflare (%s)", zone.name, outcome)
    return outcome


BLOCKED_ALERT_FA: Final = {
    ZONE_EDIT_DENIED: (
        "توکن Cloudflare دسترسی «Zone · Zone · Edit» ندارد. این دسترسی را به توکن بدهید و توکن را "
        "دوباره در صفحه‌ی Cloudflare پنل وارد کنید؛ پاک کردن پس از آن دوباره امتحان میشود."
    ),
    CF_REFUSED: (
        "Cloudflare پاک کردن آن را نپذیرفت (برای نمونه اگر دامنه در Cloudflare Registrar ثبت شده "
        "باشد). آن را خودتان در Cloudflare پاک کنید، یا در داشبورد «بازنشسته»اش کنید."
    ),
}


async def _blocked(db: Db, zone: PoolZone, now: datetime, reason: str, error: CloudflareError) -> str:
    await db.execute(
        update(CfZone)
        .where(CfZone.id == zone.id)
        .values(delete_blocked=reason[:60], delete_claimed_at=None, delete_error=error.kind, updated_at=now)
    )
    code = reason.split(":", 1)[0]
    await log_action(
        db,
        actor=ACTOR,
        action="cloudflare_zone_delete",
        target=zone.name,
        dry_run=False,
        allowed=False,
        reasons=[code],
        context={"zone_id": zone.id, "status": error.status, "codes": list(error.codes)},
        result="Cloudflare refused to delete the zone; not retried until a token is stored again",
    )
    await enqueue_owner_alert(
        db,
        f"⚠️ دامنه‌ی فیلترشده‌ی <b>{html.escape(zone.name)}</b> از Cloudflare پاک نشد. "
        + BLOCKED_ALERT_FA[code],
    )
    log.warning("domains: deleting zone %s refused: %s", zone.name, error)
    return code


async def _retry(db: Db, zone: PoolZone, now: datetime, error: CloudflareError) -> str:
    attempts = zone.delete_attempts + 1
    retry_at = now + retry_delay(attempts)
    await db.execute(
        update(CfZone)
        .where(CfZone.id == zone.id)
        .values(
            delete_claimed_at=None,
            delete_error=error.kind[:30],
            delete_attempts=attempts,
            delete_retry_at=retry_at,
            updated_at=now,
        )
    )
    await log_action(
        db,
        actor=ACTOR,
        action="cloudflare_zone_delete",
        target=zone.name,
        dry_run=False,
        allowed=True,
        reasons=[f"cf_{error.kind}"],
        context={"zone_id": zone.id, "attempts": attempts, "retry_at": iso(retry_at)},
        result=f"failed: {error.kind}; tried again later",
    )
    log.warning("domains: deleting zone %s failed (%s); retry at %s", zone.name, error, iso(retry_at))
    return "retry_later"

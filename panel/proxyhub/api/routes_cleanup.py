"""The owner's two cleaning buttons (proxyhub/cleanup). Owner only (``require_admin``, CSRF on
every write), errors ``{"error", "message_fa"}``, answers hold counts only (never a secret).

* ``POST /api/cleanup/ended/preview`` ``{"min_age_hours"}``: what «پاک کردن پایان‌یافته‌ها» would
  remove now, by table, and the ended things it keeps, by reason. It runs the purge's own
  statements in a transaction that is rolled back, so nothing changes.
* ``POST /api/cleanup/ended`` ``{"min_age_hours"}``: the purge itself, one transaction, one
  actions_log row (``cleanup.ended_purged``) with the counts.
* ``GET /api/cleanup/test-channel``: what «پاک کردن کانال آزمایشی» would do (the channel, the id
  range, the panel rows it forgets) and what blocks it, the bot's rights read live from Telegram.
* ``POST /api/cleanup/test-channel`` ``{"confirm": "پاک کن"}``: empties the TEST channel
  (decision D44); refused for the main channel in every case. One actions_log row.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.cleanup import ended, test_channel
from proxyhub.cleanup.messages import ERRORS_FA
from proxyhub.db.session import get_engine
from proxyhub.fleet.actions import log_action
from proxyhub.telegram.alerts import BotFactory
from proxyhub.telegram.client import BotClient
from proxyhub.telegram.store import now_utc
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cleanup", tags=["cleanup"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

# PostgreSQL could not take a row lock in time, found a deadlock, or ran out of statement time: a
# worker is busy with the same rows. Nothing was removed (the transaction is rolled back).
_BUSY_SQLSTATES = frozenset({"55P03", "40P01", "57014", "40001"})


def _err(code: str, status: int) -> ApiError:
    return ApiError(code, status_code=status, message_fa=ERRORS_FA.get(code, ERRORS_FA["cleanup_busy"]))


def get_vault() -> Vault:
    try:
        return Vault.from_config()
    except VaultError:
        raise _err("vault_unavailable", 503) from None


def get_bot_factory() -> BotFactory:
    """How a bot client is made (tests point it at another base URL)."""
    return BotClient


VaultDep = Annotated[Vault, Depends(get_vault)]
BotFactoryDep = Annotated[BotFactory, Depends(get_bot_factory)]


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


# --- «پاک کردن پایان‌یافته‌ها» -------------------------------------------------------------------


class EndedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_age_hours: StrictInt = Field(
        default=ended.DEFAULT_MIN_AGE_HOURS, ge=ended.MIN_AGE_HOURS, le=ended.MAX_AGE_HOURS
    )


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    for attr in ("sqlstate", "pgcode"):
        value = getattr(orig, attr, None)
        if isinstance(value, str):
            return value
    return None


async def _purge(db: AsyncSession, body: EndedIn, *, dry_run: bool) -> ended.PurgeResult:
    try:
        return await ended.purge_ended(db, now=now_utc(), min_age_hours=body.min_age_hours, dry_run=dry_run)
    except DBAPIError as exc:
        await db.rollback()
        if _sqlstate(exc) in _BUSY_SQLSTATES:
            log.warning("cleanup: ended rows busy (%s)", _sqlstate(exc))
            raise _err("cleanup_busy", 409) from None
        raise


@router.post("/ended/preview")
async def preview_ended(admin: Admin, db: Db, body: EndedIn) -> dict[str, Any]:
    try:
        result = await _purge(db, body, dry_run=True)
    finally:
        await db.rollback()  # a preview never keeps anything
    return result.as_json()


@router.post("/ended")
async def purge_ended(admin: Admin, db: Db, body: EndedIn) -> dict[str, Any]:
    result = await _purge(db, body, dry_run=False)
    await log_action(
        db,
        actor=_actor(admin),
        action="cleanup.ended_purged",
        target="database",
        dry_run=False,
        allowed=True,
        reasons=["owner_request"],
        context=result.log_context(),
        result=f"{result.total} ended row(s) removed",
    )
    await db.commit()
    return result.as_json()


# --- «پاک کردن کانال آزمایشی» (D44) ---------------------------------------------------------------


class TestChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictStr = Field(max_length=40)


@router.get("/test-channel")
async def preview_test_channel(admin: Admin, vault: VaultDep, make_bot: BotFactoryDep) -> dict[str, Any]:
    return await test_channel.preview(get_engine(), vault, make_bot)


@router.post("/test-channel")
async def clean_test_channel(
    admin: Admin, db: Db, body: TestChannelIn, vault: VaultDep, make_bot: BotFactoryDep
) -> dict[str, Any]:
    if not test_channel.confirmed(body.confirm):
        raise _err("confirm_required", 422)
    await db.rollback()  # the cleaning runs its own short transactions; none stays open here
    try:
        result = await test_channel.clean(get_engine(), vault, make_bot, actor=_actor(admin))
    except test_channel.Refused as exc:
        raise _err(exc.code, exc.status) from None
    return result.as_json()

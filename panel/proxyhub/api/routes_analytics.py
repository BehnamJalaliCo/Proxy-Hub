"""/api/analytics/*: the «آمار» page. Owner only (``require_admin``; CSRF on writes).

GET  /api/analytics/summary          this month: TB served, average per day, per server, cost, cost
                                      per TB, members now, net growth today/7d/30d, top sources,
                                      threshold warnings, collector status
GET  /api/analytics/series?days=30   daily TB, monthly TB, hourly CPU, members over time, joins and
                                      leaves per day, sources (chart data)
GET  /api/analytics/usage?days=30    proxy users from the agents' counters (D39): countries, regions,
                                      ISPs, unique users per day, MB per user (p50/p90/p99), peak hours
GET  /api/analytics/channel-stats    Telegram's channel statistics via the operator account (D38)
                                      and the views/forwards of our posts
GET  /api/analytics/capacity         per-node load signal: ok / hot / near_traffic_limit
GET  /api/analytics/links            tracking links with joins, leaves and net
POST /api/analytics/links            {"name", "purpose"} -> a new named invite link of the main channel
PUT  /api/analytics/links/{id}       {"name", "purpose"} -> new label (no delete: history stays)
GET  /api/analytics/settings         thresholds
PUT  /api/analytics/settings         thresholds (all four fields)

Errors: ``{"error", "message_fa"}``. Nothing returned here holds an IP, a person's name or a token.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.analytics import members, report
from proxyhub.analytics.messages import ERRORS_FA
from proxyhub.analytics.settings import load_settings, parse_settings, save_settings
from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.fleet.actions import log_action
from proxyhub.telegram.alerts import BotFactory
from proxyhub.telegram.client import BotClient
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/analytics", tags=["analytics"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]
Days = Annotated[int, Query(ge=1, le=report.MAX_DAYS)]


def _err(code: str, status: int) -> ApiError:
    return ApiError(code, status_code=status, message_fa=ERRORS_FA.get(code, ERRORS_FA["invalid_request"]))


def get_vault() -> Vault:
    try:
        return Vault.from_config()
    except VaultError:
        raise _err("vault_unavailable", 503) from None


def get_bot_factory() -> BotFactory:
    """How a bot client is made (tests point it at a closed local port)."""
    return BotClient


VaultDep = Annotated[Vault, Depends(get_vault)]
BotFactoryDep = Annotated[BotFactory, Depends(get_bot_factory)]


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


async def _read[T](db: AsyncSession, value: T) -> T:
    await db.rollback()  # read only; leave no transaction open
    return value


@router.get("/summary")
async def get_summary(admin: Admin, db: Db) -> dict[str, Any]:
    return await _read(db, await report.summary(db, datetime.now(UTC)))


@router.get("/series")
async def get_series(admin: Admin, db: Db, days: Days = 30) -> dict[str, Any]:
    return await _read(db, await report.series(db, datetime.now(UTC), days))


@router.get("/usage")
async def get_usage(admin: Admin, db: Db, days: Days = 30) -> dict[str, Any]:
    return await _read(db, await report.usage(db, datetime.now(UTC), days))


@router.get("/channel-stats")
async def get_channel_stats(admin: Admin, db: Db) -> dict[str, Any]:
    return await _read(db, await report.channel_stats(db))


@router.get("/capacity")
async def get_capacity(admin: Admin, db: Db) -> list[dict[str, Any]]:
    return await _read(db, await report.capacity(db, datetime.now(UTC)))


# --- tracking links ------------------------------------------------------------------------------


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    purpose: str = ""


def _link_error(exc: members.TrackingLinkError) -> ApiError:
    return _err(exc.code, exc.status)


@router.get("/links")
async def get_links(admin: Admin, db: Db) -> list[dict[str, Any]]:
    return await _read(db, await report.links(db))


@router.post("/links", status_code=201)
async def post_link(
    admin: Admin, body: LinkIn, db: Db, vault: VaultDep, make_bot: BotFactoryDep
) -> dict[str, Any]:
    try:
        row = await members.create_tracking_link(
            db, body.name, body.purpose, vault=vault, bot_factory=make_bot, actor=_actor(admin)
        )
    except members.TrackingLinkError as exc:
        raise _link_error(exc) from None
    return row.as_json() | {"joins": 0, "leaves": 0, "net": 0, "joins_7d": 0, "last_join_at": None}


@router.put("/links/{link_id}")
async def put_link(admin: Admin, link_id: int, body: LinkIn, db: Db) -> dict[str, Any]:
    try:
        await members.rename_tracking_link(db, link_id, body.name, body.purpose, actor=_actor(admin))
    except members.TrackingLinkError as exc:
        raise _link_error(exc) from None
    for item in await report.links(db):
        if item["id"] == link_id:
            return await _read(db, item)
    raise _err("link_not_found", 404)


# --- thresholds ----------------------------------------------------------------------------------


@router.get("/settings")
async def get_settings(admin: Admin, db: Db) -> dict[str, Any]:
    return await _read(db, (await load_settings(db)).model_dump())


@router.put("/settings")
async def put_settings(admin: Admin, db: Db, payload: Annotated[Any, Body()]) -> dict[str, Any]:
    try:
        new = parse_settings(payload)
    except ValueError:  # pydantic's ValidationError is a ValueError
        raise _err("invalid_settings", 422) from None
    old = await load_settings(db)
    await save_settings(db, new)
    await log_action(
        db,
        actor=_actor(admin),
        action="analytics.settings_set",
        target="analytics",
        dry_run=False,
        allowed=True,
        context={"old": old.model_dump(), "new": new.model_dump()},
    )
    await db.commit()
    return new.model_dump()

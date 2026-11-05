"""Proxy Sponsor and @MTProxybot registrations (docs/11 section 2, decisions D25 and D37).

GET    /api/sponsor                               overview: module status, pacing, registrations
                                                  (state, sponsor channel, the bot's last reply when
                                                  stuck), campaigns, settings
PUT    /api/sponsor/settings                      {default_channel, lead_minutes}
POST   /api/sponsor/registrations/{id}/retry      «ثبت دوباره»
POST   /api/sponsor/campaigns                     {customer?, channel, starts_at, ends_at, price?,
                                                  currency?, paid}
PUT    /api/sponsor/campaigns/{id}                the same fields (an active campaign keeps its start)
POST   /api/sponsor/campaigns/{id}/cancel         back to the default sponsor on the next pass
DELETE /api/sponsor/campaigns/{id}                only a campaign that never started
POST   /api/sponsor/mtproxybot/inventory          read @MTProxybot's own list and each live proxy's
                                                  sponsor (a job the sponsor loop runs)
POST   /api/sponsor/mtproxybot/cleanup            «پاک کردن پروکسی‌های مرده از ‎@MTProxybot‎»: only
                                                  proxies whose node was deleted; "observe" (nothing
                                                  pressed) until removal is turned on from the CLI
POST   /api/sponsor/mtproxybot/stop               end the open job

Owner only (``require_admin``: session cookie and CSRF on every write). Errors are
``{"error", "message_fa"}``. No response holds a proxy secret: tags are shortened, the bot's last
reply was masked before it was stored, and @MTProxybot's list holds addresses, label shapes and
screen shapes only. Nothing here talks to Telegram; the reconciler does.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
)

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, require_admin
from proxyhub.db.session import get_engine
from proxyhub.sponsor import inventory, service
from proxyhub.sponsor import provider as provider_module
from proxyhub.sponsor.inventory import InventoryError
from proxyhub.sponsor.messages import STATUS, error_message
from proxyhub.sponsor.service import CampaignInput, SponsorError

router = APIRouter(prefix="/api/sponsor", tags=["sponsor"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]

MAX_PRICE = Decimal("1000000000")
_CURRENCY = re.compile(r"[A-Z]{3,8}")


def _error(code: str) -> ApiError:
    return ApiError(code, status_code=STATUS.get(code, 400), message_fa=error_message(code))


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise _error("not_found") from None


async def _overview() -> dict[str, Any]:
    engine = get_engine()
    view = await service.overview(
        engine, now=_now(), provider_ready=provider_module.MTPROTO_PROVIDER is not None
    )
    view["mtproxybot"] = await inventory.overview(engine, wanted=str(view["wanted_channel"]))
    return view


@router.get("")
async def get_sponsor(admin: Admin) -> dict[str, Any]:
    return await _overview()


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_channel: StrictStr = Field(max_length=200)
    lead_minutes: StrictInt


@router.put("/settings")
async def put_settings(admin: Admin, body: SettingsIn) -> dict[str, Any]:
    try:
        await service.save_settings(
            get_engine(),
            default_channel=body.default_channel,
            lead_minutes=body.lead_minutes,
            actor=_actor(admin),
        )
    except SponsorError as exc:
        raise _error(exc.code) from None
    return await _overview()


@router.post("/registrations/{reg_id}/retry")
async def retry_registration(admin: Admin, reg_id: str) -> dict[str, Any]:
    try:
        await service.retry(get_engine(), _uuid(reg_id), actor=_actor(admin), now=_now())
    except SponsorError as exc:
        raise _error(exc.code) from None
    return await _overview()


class CampaignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer: StrictStr | None = Field(default=None, max_length=400)
    channel: StrictStr = Field(max_length=200)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    price: StrictStr | StrictInt | StrictFloat | None = None
    currency: StrictStr | None = Field(default=None, max_length=16)
    paid: StrictBool = False


def _price(value: str | int | float | None) -> Decimal | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        price = Decimal(str(value).strip())
    except InvalidOperation:
        raise _error("price_format") from None
    exponent = price.as_tuple().exponent
    if (
        not price.is_finite()
        or price < 0
        or price > MAX_PRICE
        or (isinstance(exponent, int) and exponent < -2)
    ):
        raise _error("price_format")
    return price.quantize(Decimal("0.01"))


def _currency(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    code = value.strip().upper()
    if not _CURRENCY.fullmatch(code):
        raise _error("currency_format")
    return code


def _campaign_input(body: CampaignIn) -> CampaignInput:
    return CampaignInput(
        customer=body.customer,
        channel=body.channel,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        price=_price(body.price),
        currency=_currency(body.currency),
        paid=body.paid,
    )


@router.post("/campaigns")
async def create_campaign(admin: Admin, body: CampaignIn) -> dict[str, Any]:
    data = _campaign_input(body)
    try:
        campaign_id = await service.create_campaign(get_engine(), data, actor=_actor(admin), now=_now())
    except SponsorError as exc:
        raise _error(exc.code) from None
    return {"id": str(campaign_id), **(await _overview())}


@router.put("/campaigns/{campaign_id}")
async def update_campaign(admin: Admin, campaign_id: str, body: CampaignIn) -> dict[str, Any]:
    data = _campaign_input(body)
    try:
        await service.update_campaign(get_engine(), _uuid(campaign_id), data, actor=_actor(admin), now=_now())
    except SponsorError as exc:
        raise _error(exc.code) from None
    return await _overview()


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(admin: Admin, campaign_id: str) -> dict[str, Any]:
    try:
        await service.cancel_campaign(get_engine(), _uuid(campaign_id), actor=_actor(admin), now=_now())
    except SponsorError as exc:
        raise _error(exc.code) from None
    return await _overview()


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(admin: Admin, campaign_id: str) -> dict[str, Any]:
    try:
        await service.delete_campaign(get_engine(), _uuid(campaign_id), actor=_actor(admin), now=_now())
    except SponsorError as exc:
        raise _error(exc.code) from None
    return await _overview()


@router.post("/mtproxybot/inventory")
async def mtproxybot_inventory(admin: Admin) -> dict[str, Any]:
    try:
        await inventory.request_job(get_engine(), inventory.INVENTORY, actor=_actor(admin), now=_now())
    except InventoryError as exc:
        raise _error(exc.code) from None
    return await _overview()


@router.post("/mtproxybot/cleanup")
async def mtproxybot_cleanup(admin: Admin) -> dict[str, Any]:
    try:
        await inventory.request_job(get_engine(), inventory.CLEANUP, actor=_actor(admin), now=_now())
    except InventoryError as exc:
        raise _error(exc.code) from None
    return await _overview()


@router.post("/mtproxybot/stop")
async def mtproxybot_stop(admin: Admin) -> dict[str, Any]:
    try:
        await inventory.stop_job(get_engine(), actor=_actor(admin), now=_now())
    except InventoryError as exc:
        raise _error(exc.code) from None
    return await _overview()

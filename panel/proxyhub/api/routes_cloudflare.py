"""Cloudflare DNS and the domain pool (docs/10 section 3.10, decision D13).

GET /api/cloudflare/status, PUT /api/cloudflare/token, PUT /api/cloudflare/pool. The token is
verified for real against Cloudflare, stored only in the vault, and never returned or logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.cloudflare import service
from proxyhub.cloudflare.pool import PoolState
from proxyhub.vault import Vault, VaultKeyError

router = APIRouter(prefix="/api/cloudflare", tags=["cloudflare"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

MESSAGES_FA: dict[str, str] = {
    "cf_token_invalid": (
        "توکن Cloudflare معتبر نیست، باطل شده یا زمانش گذشته است. یک توکن تازه بسازید و دوباره وارد کنید."
    ),
    "cf_permissions_missing": (
        "این توکن همه‌ی دسترسی‌های لازم را ندارد و ذخیره نشد. توکن باید روی دامنه‌ی پروکسی "
        "دو دسترسی «Zone → Zone → Read» و «Zone → DNS → Edit» داشته باشد."
    ),
    "cf_token_too_broad": (
        "این توکن بیش از یک دامنه را می‌بیند و ذخیره نشد. در Cloudflare، در بخش Zone Resources "
        "گزینه‌ی «Specific zone» را بزنید و فقط دامنه‌ی پروکسی را انتخاب کنید."
    ),
    "cf_unavailable": "پاسخ API کلودفلر به دست نیامد. چند دقیقه‌ی دیگر دوباره امتحان کنید.",
    "vault_unavailable": "کلید رمزنگاری پنل در دسترس نیست؛ توکن ذخیره نشد.",
    "not_found": "این دامنه در فهرست zoneهایی که توکن Cloudflare می‌بیند نیست.",
    "pool_transition_invalid": "این تغییر وضعیت برای این دامنه در وضعیت فعلی‌اش مجاز نیست.",
    "zone_not_ready": (
        "این دامنه هنوز در Cloudflare فعال نیست (nameserverهای دامنه هنوز روی Cloudflare نیستند) "
        "یا توکن روی آن دسترسی ویرایش DNS ندارد؛ پس نمی‌شود آن را دامنه‌ی فعال کرد."
    ),
}
STATUS: dict[str, int] = {
    "cf_token_invalid": 400,
    "cf_permissions_missing": 400,
    "cf_token_too_broad": 400,
    "cf_unavailable": 502,
    "vault_unavailable": 503,
    "not_found": 404,
    "pool_transition_invalid": 409,
    "zone_not_ready": 409,
}


def _error(code: str, **extra: Any) -> ApiError:
    return ApiError(code, status_code=STATUS.get(code, 400), message_fa=MESSAGES_FA.get(code), extra=extra)


def cloudflare_vault() -> Vault:
    try:
        return Vault.from_config()
    except VaultKeyError:
        raise _error("vault_unavailable") from None


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1, max_length=512)


class PoolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zone_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    pool_state: Literal["none", "active", "standby", "filtered", "retired"]


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


@router.get("/status")
async def cloudflare_status(admin: Admin, db: Db) -> dict[str, Any]:
    return await service.status(db)


@router.put("/token")
async def put_token(
    admin: Admin, body: TokenIn, db: Db, vault: Annotated[Vault, Depends(cloudflare_vault)]
) -> dict[str, Any]:
    try:
        return await service.set_token(
            db, vault, body.token.get_secret_value(), actor=_actor(admin), now=datetime.now(UTC)
        )
    except service.CfServiceError as exc:
        extra = {k: v for k, v in exc.extra.items() if k in ("missing", "zones_seen", "zones_allowed")}
        raise _error(exc.code, **extra) from None


@router.put("/pool")
async def put_pool(admin: Admin, body: PoolIn, db: Db) -> dict[str, Any]:
    try:
        return await service.set_pool_state(
            db, body.zone_id, PoolState(body.pool_state), actor=_actor(admin), now=datetime.now(UTC)
        )
    except service.CfServiceError as exc:
        raise _error(exc.code) from None

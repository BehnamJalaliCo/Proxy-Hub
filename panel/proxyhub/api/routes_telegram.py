"""Telegram API (round-2 contract) and the internal Alertmanager webhook.

/api/telegram/* : owner only (``require_admin``, CSRF on writes), errors ``{"error", "message_fa"}``.
/internal/alerts/alertmanager : Alertmanager's webhook receiver on the backend network (Caddy
answers 404 for /internal/* publicly). It sends the alert to the owner DIRECTLY, not through the
outbox, so a stopped worker cannot hold back the alert about the stopped worker.

The bot token goes in once (``PUT /api/telegram/bot``), is checked with getMe, stored in the vault
and never returned, logged or written to actions_log (CLAUDE.md rule 3).

Documented additions to the contract: status has ``bot_error`` (null, ``"bot_unauthorized"`` or
``"updates_conflict"``)
and each Channel has ``chat_id``; the pairing answer has ``link`` (``https://t.me/<bot>?start=<code>``);
``PUT /channel`` also accepts a private channel's numeric id (``-100...``) as ``username`` and may
answer 400 ``channel_in_use`` or ``bot_token_invalid``; Telegram being unreachable is 502
``telegram_unavailable``; ``POST /pairing`` without a bot is 409 ``bot_not_set``. A body of the
wrong shape (no ``token`` string, a ``role`` other than main/test, unknown fields) is the API-wide
422 ``invalid_request``; any token string that is not a Bot API token (empty, overlong, wrong
characters) is 400 ``bot_token_invalid``, and a settings body that is not valid JSON is 422
``invalid_settings``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.fleet.actions import iso, log_action
from proxyhub.telegram import pairing, store
from proxyhub.telegram.alerts import (
    ALERT_DEDUPER,
    ALERT_TARGETS,
    AlertmanagerWebhook,
    BotFactory,
    NotConfigured,
    OwnerTarget,
    render_alertmanager_message,
    send_direct,
)
from proxyhub.telegram.client import (
    BadRequest,
    BotClient,
    Forbidden,
    InvalidToken,
    NotFound,
    TelegramError,
    Unauthorized,
    as_secret,
)
from proxyhub.telegram.messages import ERRORS_FA, TEST_ALERT_FA, TEST_POST_FA
from proxyhub.telegram.schedule import (
    TEHRAN,
    InvalidSchedule,
    QuietWindow,
    validate_interval,
    validate_windows,
)
from proxyhub.telegram.templates import fa_digits, render_proxy_post
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

PAIRING_CACHE_KEY = "proxyhub:telegram:pairing"
_CHAT_ID_RE = re.compile(r"^-100[0-9]{5,16}$")
_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_ALERT_LOCK = asyncio.Lock()

api = APIRouter(prefix="/api/telegram", tags=["telegram"], route_class=ApiRoute)
internal = APIRouter(prefix="/internal", include_in_schema=False)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]


def _err(code: str, status: int) -> ApiError:
    return ApiError(code, status_code=status, message_fa=ERRORS_FA.get(code, ERRORS_FA["invalid_request"]))


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


# --- pairing code redisplay (Redis memory only; the database holds only its hash) ----------------


async def _remember_code(request: Request, pairing_id: int, code: str, ttl_s: int) -> None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    try:
        await redis.set(PAIRING_CACHE_KEY, json.dumps({"id": pairing_id, "code": code}), ex=max(1, ttl_s))
    except Exception as exc:  # the code was already shown to the owner; redisplay is a convenience
        log.warning("telegram: pairing code not cached for redisplay (%s)", type(exc).__name__)


async def _cached_code(request: Request, pairing_id: int) -> str | None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return None
    try:
        raw = await redis.get(PAIRING_CACHE_KEY)
        data = json.loads(raw) if raw else None
    except Exception:
        return None
    if isinstance(data, dict) and data.get("id") == pairing_id and isinstance(data.get("code"), str):
        return str(data["code"])
    return None


# --- status --------------------------------------------------------------------------------------


async def telegram_status(db: AsyncSession, request: Request) -> dict[str, Any]:
    now = store.now_utc()
    bot = await store.bot_info(db)
    owner = await store.owner_chat_id(db)
    open_pairing = await store.open_pairing(db, now)
    channels = await store.channels(db)
    pending, failed = await store.outbox_counts(db)
    last_alert = await store.get_setting(db, store.KEY_LAST_ALERT) or {}
    health = await store.get_setting(db, store.KEY_HEALTH) or {}
    updates = await store.get_setting(db, store.KEY_UPDATES) or {}
    await db.rollback()  # read only; leave no transaction open
    pairing_out = None
    if open_pairing is not None:
        code = await _cached_code(request, open_pairing.id)
        if code is not None:
            pairing_out = {"code": code, "expires_at": iso(open_pairing.expires_at)}
    return {
        "bot": {"username": bot.username, "id": bot.id} if bot else None,
        "owner_paired": bot is not None and owner is not None,
        "pairing": pairing_out,
        "channels": {role: store.public_channel(channels.get(role)) for role in store.ROLES},
        "outbox": {"pending": pending, "failed": failed},
        "last_alert_at": last_alert.get("at"),
        # bot_unauthorized (token revoked) or updates_conflict (another poller/webhook: pairing
        # cannot work) or null.
        "bot_error": (health.get("error") or _conflict(updates)) if bot else None,
    }


def _conflict(updates: dict[str, Any]) -> str | None:
    return "updates_conflict" if updates.get("error") == "updates_conflict" else None


@api.get("/status")
async def get_status(admin: Admin, db: Db, request: Request) -> dict[str, Any]:
    return await telegram_status(db, request)


# --- bot token -----------------------------------------------------------------------------------


class BotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # No length limits here: an empty or overlong token is a wrong token (400 bot_token_invalid,
    # from the shape check below), not a malformed request.
    token: SecretStr


@api.put("/bot")
async def put_bot(
    admin: Admin, body: BotIn, db: Db, request: Request, vault: VaultDep, make_bot: BotFactoryDep
) -> dict[str, Any]:
    try:
        token = as_secret(body.token.get_secret_value().strip())
    except (InvalidToken, ValueError):
        raise _err("bot_token_invalid", 400) from None
    try:
        async with make_bot(token) as bot:
            me = await bot.get_me()
    except (Unauthorized, NotFound):
        raise _err("bot_token_invalid", 400) from None
    except TelegramError as exc:
        log.warning("telegram: getMe failed while setting the bot (%s)", exc)
        raise _err("telegram_unavailable", 502) from None
    now = store.now_utc()
    new_bot = await store.save_bot(
        db, vault, info=store.BotInfo(id=me.id, username=me.username), token=token, now=now
    )
    await log_action(
        db,
        actor=_actor(admin),
        action="telegram.bot_set",
        target=f"@{me.username}",
        dry_run=False,
        allowed=True,
        reasons=["getme_ok"],
        context={"bot_id": me.id, "new_bot": new_bot},
    )
    await db.commit()
    ALERT_TARGETS.clear()
    return await telegram_status(db, request)


# --- owner pairing -------------------------------------------------------------------------------


@api.post("/pairing")
async def create_pairing(admin: Admin, db: Db, request: Request) -> dict[str, Any]:
    bot = await store.bot_info(db)
    if bot is None:
        raise _err("bot_not_set", 409)
    now = store.now_utc()
    code = pairing.new_code()
    created = await store.create_pairing(db, code, now)
    await log_action(
        db,
        actor=_actor(admin),
        action="telegram.pairing_created",
        target=f"@{bot.username}",
        dry_run=False,
        allowed=True,
        context={"expires_at": iso(created.expires_at)},
    )
    await db.commit()
    await _remember_code(request, created.id, code, int(pairing.TTL.total_seconds()))
    return {
        "code": code,
        "expires_at": iso(created.expires_at),
        "link": f"https://t.me/{bot.username}?start={code}",
    }


# --- channels ------------------------------------------------------------------------------------


class ChannelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["main", "test"]
    username: str = Field(min_length=1, max_length=64)


def chat_ref(value: str) -> str | int | None:
    """``@name`` / ``name`` -> ``"@name"``; ``-100...`` (a private channel's id) -> int; else None."""
    value = value.strip()
    if _CHAT_ID_RE.fullmatch(value):
        return int(value)
    m = _USERNAME_RE.fullmatch(value)
    return f"@{m.group(1)}" if m else None


@api.put("/channel")
async def put_channel(
    admin: Admin, body: ChannelIn, db: Db, request: Request, vault: VaultDep, make_bot: BotFactoryDep
) -> dict[str, Any]:
    ref = chat_ref(body.username)
    if ref is None:
        raise _err("channel_not_found", 400)
    creds = await store.load_bot(db, vault)
    if creds is None:
        raise _err("bot_not_set", 400)
    await db.rollback()  # no transaction open while Telegram answers
    try:
        async with make_bot(creds.token) as bot:
            chat = await bot.get_chat(ref)
            if chat.type != "channel":
                raise _err("channel_not_found", 400)
            rights = await bot.get_chat_member(chat.id, creds.info.id)
    except BadRequest:
        raise _err("channel_not_found", 400) from None
    except Forbidden:
        raise _err("bot_not_admin", 400) from None
    except Unauthorized:
        raise _err("bot_token_invalid", 400) from None
    except TelegramError as exc:
        log.warning("telegram: channel check failed (%s)", exc)
        raise _err("telegram_unavailable", 502) from None
    if rights.status not in ("administrator", "creator"):
        raise _err("bot_not_admin", 400)
    if not rights.can_post:
        raise _err("bot_cannot_post", 400)
    if not rights.can_edit:
        raise _err("bot_cannot_edit", 400)
    other = (await store.channels(db)).get("test" if body.role == "main" else "main")
    if other is not None and other.get("chat_id") == chat.id:
        raise _err("channel_in_use", 400)
    now = store.now_utc()
    username = f"@{chat.username}" if chat.username else str(chat.id)
    channel = {
        "chat_id": chat.id,
        "username": username,
        "title": chat.title or "",
        "can_post": rights.can_post,
        "can_edit": rights.can_edit,
        "can_delete": rights.can_remove,
        "checked_at": iso(now),
    }
    await store.save_channel(db, body.role, channel)
    await log_action(
        db,
        actor=_actor(admin),
        action="telegram.channel_set",
        target=f"{body.role}:{username}",
        dry_run=False,
        allowed=True,
        reasons=["bot_can_delete"] if rights.can_remove else [],
        context={"can_post": rights.can_post, "can_edit": rights.can_edit, "can_delete": rights.can_remove},
    )
    await db.commit()
    return await telegram_status(db, request)


# --- posting settings (D21) ----------------------------------------------------------------------


def _parse_settings(body: Any) -> store.PostingSettings:
    if not isinstance(body, dict) or set(body) != {"quiet_windows", "post_interval_min"}:
        raise InvalidSchedule("expected exactly quiet_windows and post_interval_min")
    raw_windows, interval = body["quiet_windows"], body["post_interval_min"]
    if not isinstance(raw_windows, list) or isinstance(interval, bool) or not isinstance(interval, int):
        raise InvalidSchedule("wrong types")
    windows: list[QuietWindow] = []
    for item in raw_windows:
        if not isinstance(item, dict) or set(item) != {"start", "end"}:
            raise InvalidSchedule("a window is {start, end}")
        if not isinstance(item["start"], str) or not isinstance(item["end"], str):
            raise InvalidSchedule("times are strings")
        windows.append(QuietWindow.parse(item["start"], item["end"]))
    return store.PostingSettings(
        quiet_windows=validate_windows(windows), post_interval_min=validate_interval(interval)
    )


@api.get("/settings")
async def get_settings(admin: Admin, db: Db) -> dict[str, Any]:
    settings = await store.posting_settings(db)
    await db.rollback()
    return settings.as_json()


@api.put("/settings")
async def put_settings(admin: Admin, db: Db, request: Request) -> dict[str, Any]:
    # The body is read here, not by FastAPI, so that a body that is not even JSON is also the
    # contract's 422 invalid_settings (not the generic invalid_request).
    try:
        settings = _parse_settings(json.loads(await request.body()))
    except (ValueError, RecursionError):  # not JSON, too deeply nested, or InvalidSchedule
        raise _err("invalid_settings", 422) from None
    old = await store.posting_settings(db)
    await store.save_posting_settings(db, settings)
    await log_action(
        db,
        actor=_actor(admin),
        action="telegram.settings_set",
        target="telegram.posting",
        dry_run=False,
        allowed=True,
        context={"old": old.as_json(), "new": settings.as_json()},
    )
    await db.commit()
    return settings.as_json()


# --- tests the owner can run from the dashboard --------------------------------------------------


@api.post("/test-alert", status_code=202)
async def test_alert(admin: Admin, db: Db) -> dict[str, Any]:
    if await store.bot_info(db) is None:
        raise _err("bot_not_set", 409)
    if await store.owner_chat_id(db) is None:
        raise _err("owner_not_paired", 409)
    job = await store.enqueue(db, "alert", {"text": TEST_ALERT_FA})
    await log_action(
        db,
        actor=_actor(admin),
        action="telegram.test_alert",
        target=f"outbox:{job}",
        dry_run=False,
        allowed=True,
    )
    await db.commit()
    return {}


def render_test_post(main_username: str | None) -> str:
    clock = fa_digits(store.now_utc().astimezone(TEHRAN).strftime("%Y-%m-%d %H:%M"))
    tag = main_username if main_username and main_username.startswith("@") else store.DEFAULT_CHANNEL_TAG
    return render_proxy_post(f"{TEST_POST_FA}\n⏱ {clock} (تهران)", [], tag)


@api.post("/test-post", status_code=202)
async def test_post(admin: Admin, db: Db) -> dict[str, Any]:
    if await store.bot_info(db) is None:
        raise _err("bot_not_set", 409)
    channels = await store.channels(db)
    if "test" not in channels:
        raise _err("test_channel_not_set", 409)
    main = channels.get("main")
    text = render_test_post(str(main["username"]) if main else None)
    job = await store.enqueue(db, "send_post", {"role": "test", "text": text, "post_kind": "test"})
    await log_action(
        db,
        actor=_actor(admin),
        action="telegram.test_post",
        target=f"outbox:{job}",
        dry_run=False,
        allowed=True,
    )
    await db.commit()
    return {"outbox_id": job}


# --- Alertmanager webhook ------------------------------------------------------------------------


def _internal_error(code: str, status: int, message_fa: str) -> JSONResponse:
    return JSONResponse({"error": code, "message_fa": message_fa}, status_code=status)


@internal.post("/alerts/alertmanager")
async def alertmanager_webhook(
    payload: AlertmanagerWebhook, db: Db, vault: VaultDep, make_bot: BotFactoryDep
) -> JSONResponse:
    """Status codes for Alertmanager: 200 delivered (or nothing new); 409 nobody to send to or
    Telegram refused (no retry helps); 503 temporary failure (Alertmanager retries)."""

    async def load_target() -> OwnerTarget:
        creds = await store.load_bot(db, vault)
        if creds is None:
            raise NotConfigured("bot_not_set")
        chat_id = await store.owner_chat_id(db)
        if chat_id is None:
            raise NotConfigured("owner_not_paired")
        await db.rollback()
        return OwnerTarget(token=creds.token, chat_id=chat_id)

    async with _ALERT_LOCK:
        now = store.now_utc()
        fresh = ALERT_DEDUPER.fresh(payload.alerts, now)
        if not fresh:
            return JSONResponse({"delivered": 0, "duplicates": len(payload.alerts)})
        try:
            target = await ALERT_TARGETS.get(load_target)
        except NotConfigured as exc:
            return _internal_error(exc.code, 409, ERRORS_FA[exc.code])
        except Exception as exc:  # database down and nothing cached yet
            log.warning("alertmanager webhook: owner chat unknown (%s)", type(exc).__name__)
            return _internal_error("database_unavailable", 503, ERRORS_FA["telegram_unavailable"])
        text = render_alertmanager_message(fresh, truncated=payload.truncated_alerts)
        try:
            await send_direct(make_bot, target, text)
        except (Unauthorized, Forbidden, BadRequest) as exc:
            log.warning("alertmanager webhook: Telegram refused the alert (%s)", exc)
            return _internal_error("telegram_rejected", 409, ERRORS_FA["bot_token_invalid"])
        except TelegramError as exc:
            log.warning("alertmanager webhook: alert not delivered yet (%s)", exc)
            return _internal_error("telegram_unavailable", 503, ERRORS_FA["telegram_unavailable"])
        ALERT_DEDUPER.mark(fresh, now)
    try:
        await store.mark_alert_delivered(db, now)
        await db.commit()
    except Exception as exc:  # delivered; only the dashboard's "last alert" time is missing
        log.warning("alertmanager webhook: last_alert_at not recorded (%s)", type(exc).__name__)
    return JSONResponse({"delivered": len(fresh), "duplicates": len(payload.alerts) - len(fresh)})


router = APIRouter()
router.include_router(api)
router.include_router(internal)

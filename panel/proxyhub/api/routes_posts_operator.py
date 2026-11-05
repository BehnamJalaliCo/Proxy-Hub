"""The operator poster on the Content page (decision D38): posts with animated Premium emoji.

GET  /api/posts/operator         who posts, the emoji pack and its emoji, the account's rights in each
                                 channel (with warnings), why the operator cannot post right now, the
                                 recent posts that went with the bot instead (Persian), and the
                                 channel's own pack (``own_pack``, proxyhub/posts/own_pack.py)
PUT  /api/posts/operator         ``{"poster": "bot" | "operator", "pack_link": "https://t.me/addemoji/..."}``
                                 (either key; an empty or null pack link removes the pack, and the
                                 channel's own pack takes its place within a minute)
POST /api/posts/operator/check   read the pack and the account's rights NOW (under the account's lock)

Owner only (``require_admin``; CSRF on writes). Errors are ``{"error", "message_fa"}``:

===========================  ======  ===================================================================
``invalid_request``          422     not a JSON object of the keys above
``poster_invalid``           422     poster is neither ``bot`` nor ``operator``
``pack_link_invalid``        422     not a ``t.me/addemoji/<name>`` link (``pack_link_stickers``: a
                                     sticker pack's link)
``mtproxy_busy``             409     another operation on the account holds its lock
``operator_kill_switch``     409     the fleet's kill switch is ON
``operator_not_connected``   409     the operator account is not connected
``operator_unavailable``     502     Telegram did not answer the account
``vault_unavailable``        503     the panel's master key cannot be read
===========================  ======  ===================================================================

No response holds a secret, a session or a phone. Nothing here deletes anything (rule 6).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.api.routes_mtproxy_account import get_client_options, get_lock_wait_s, get_vault
from proxyhub.db.session import get_engine
from proxyhub.fleet.actions import log_action
from proxyhub.mtproxy.userclient import ClientOptions
from proxyhub.posts import operator, operator_checks, own_pack
from proxyhub.posts.messages import ERRORS_FA
from proxyhub.telegram import store as tg_store
from proxyhub.vault import Vault

router = APIRouter(prefix="/api/posts/operator", tags=["posts"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

STATUS: dict[str, int] = {
    "invalid_request": 422,
    "poster_invalid": 422,
    "pack_link_invalid": 422,
    "pack_link_stickers": 422,
    "mtproxy_busy": 409,
    "operator_kill_switch": 409,
    "operator_not_connected": 409,
    "operator_unavailable": 502,
    "vault_unavailable": 503,
}
# run_checks outcome -> API error
_CHECK_ERRORS: dict[str, str] = {
    "busy": "mtproxy_busy",
    "kill_switch": "operator_kill_switch",
    "not_connected": "operator_not_connected",
    "vault_unavailable": "vault_unavailable",
}


def _err(code: str, **extra: Any) -> ApiError:
    return ApiError(
        code,
        status_code=STATUS.get(code, 400),
        message_fa=ERRORS_FA.get(code, ERRORS_FA["invalid_request"]),
        extra=extra or None,
    )


async def _view() -> dict[str, Any]:
    async with get_engine().connect() as conn:
        return {
            **await operator.view(conn, tg_store.now_utc()),
            "own_pack": await own_pack.status(conn),
        }


@router.get("")
async def get_operator(admin: Admin) -> dict[str, Any]:
    return await _view()


@router.put("")
async def put_operator(admin: Admin, db: Db, request: Request) -> dict[str, Any]:
    try:
        value = json.loads(await request.body())
    except (ValueError, RecursionError):
        raise _err("invalid_request") from None
    if not isinstance(value, dict) or not value or not set(value) <= {"poster", "pack_link"}:
        raise _err("invalid_request")
    old = await operator.load_config(db)
    poster = value.get("poster", old.poster)
    if poster not in operator.POSTERS:
        raise _err("poster_invalid")
    pack = old.emoji_pack
    if "pack_link" in value:
        link = value["pack_link"]
        if link is None or (isinstance(link, str) and not link.strip()):
            pack = None
        elif not isinstance(link, str) or len(link) > 300:
            raise _err("pack_link_invalid")
        else:
            try:
                pack = operator.parse_pack_link(link)
            except operator.PackLinkInvalid as exc:
                raise _err(exc.code) from None
    new = operator.OperatorConfig(poster=poster, emoji_pack=pack)
    if new != old:
        await operator.save_config(db, new)
        changed = {k: [v, new.as_json()[k]] for k, v in old.as_json().items() if new.as_json()[k] != v}
        await log_action(
            db,
            actor=f"admin:{admin.username}",
            action="posts.operator_set",
            target="posts.operator",
            dry_run=False,
            allowed=True,
            reasons=["poster_operator"] if new.poster == "operator" and old.poster != "operator" else [],
            context={"changed": changed},
        )
    await db.commit()
    return await _view()


@router.post("/check")
async def check_now(
    admin: Admin,
    vault: Annotated[Vault, Depends(get_vault)],
    options: Annotated[ClientOptions, Depends(get_client_options)],
    lock_wait_s: Annotated[float, Depends(get_lock_wait_s)],
) -> dict[str, Any]:
    outcome = await operator_checks.run_checks(
        get_engine(), vault, now=tg_store.now_utc(), options=options, force=True, lock_wait_s=lock_wait_s
    )
    if outcome in _CHECK_ERRORS:
        raise _err(_CHECK_ERRORS[outcome])
    if outcome.startswith("failed:"):
        raise _err("operator_unavailable", telegram_error=outcome.removeprefix("failed:"))
    return {**await _view(), "outcome": outcome}

"""The operator poster's two daily reads (decision D38): the owner's emoji pack and the account's rights.

Once a day (an hour after a failed try), and at once when the owner presses "check now" on the
Content page, under the operator account's lock and only while the fleet's kill switch is OFF and
the account is connected, ONE connection reads:

* ``messages.getStickerSet`` of the ONE pack the owner pasted (``https://t.me/addemoji/<name>``):
  its title and every emoji's document id with its ``alt`` emoji (``posts.operator.pack``). A
  sticker pack that is not a custom-emoji pack is refused (``not_emoji_pack``). A pack read for an
  earlier link is never used for posts (proxyhub/posts/operator.py, :func:`emoji_map`).
* for each configured channel with a public @username: ``contacts.resolveUsername`` (the id must
  be the configured one) and ``channels.getParticipant`` of the account ITSELF: may it post and
  edit, and does it (wrongly) have the delete right (``posts.operator.rights``; the Content page
  warns about a missing post or edit right and about a present delete right).

The reads run while the owner wants the operator (poster ``operator``) or has pasted a pack, so the
page can show both before the owner switches. Nothing is written to Telegram here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.fleet.actions import iso
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy.channel_stats import bare_channel_id
from proxyhub.mtproxy.userclient import ChannelScope, ClientOptions, MtpError, operator_session
from proxyhub.posts import operator
from proxyhub.telegram import store
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

EVERY: Final = timedelta(days=1)
AFTER_FAILURE: Final = timedelta(hours=1)
DEFAULT_LOCK_WAIT_S: Final = 5.0


@dataclass(frozen=True)
class Channel:
    role: str
    chat_id: int
    channel_id: int | None
    username: str | None


def _due(last: datetime | None, failed: bool, now: datetime) -> bool:
    return last is None or now - last >= (AFTER_FAILURE if failed else EVERY)


def _channels(channels: dict[str, dict[str, Any]]) -> list[Channel]:
    out: list[Channel] = []
    for role in store.ROLES:
        ch = channels.get(role)
        chat_id = (ch or {}).get("chat_id")
        if isinstance(chat_id, int):
            out.append(Channel(role, chat_id, bare_channel_id(chat_id), operator.public_username(ch)))
    return out


def _rights_due(rights: dict[str, Any], targets: list[Channel], now: datetime) -> bool:
    checked = operator.parse_time(rights.get("checked_at"))
    stored = rights.get("channels") or {}
    if any((stored.get(t.role) or {}).get("chat_id") != t.chat_id for t in targets):
        return True  # a channel was set or changed since
    failed = any((v or {}).get("status") == "error" for v in stored.values())
    return _due(checked, failed, now)


async def run_checks(
    engine: AsyncEngine,
    vault: Vault,
    *,
    now: datetime,
    options: ClientOptions | None = None,
    force: bool = False,
    lock_wait_s: float = DEFAULT_LOCK_WAIT_S,
) -> str:
    """Read the pack and the rights when due (``force``: now). Returns ``idle``, ``not_due``,
    ``nothing_to_check`` (no pack and no channel), ``kill_switch``, ``not_connected``, ``busy``,
    ``vault_unavailable``, ``ok``, ``partial`` or ``failed:<code>``."""
    async with engine.connect() as conn:
        config = await operator.load_config(conn)
        pack = await operator.load_pack(conn)
        rights = await operator.load_rights(conn)
        targets = _channels(await store.channels(conn))
    if not force and config.poster != "operator" and config.emoji_pack is None:
        return "idle"
    want_pack = config.emoji_pack is not None and (
        force
        or pack is None
        or not operator.same_pack(pack.short_name, config.emoji_pack)
        or _due(pack.attempted_at, pack.error is not None, now)
    )
    want_rights = bool(targets) and (force or _rights_due(rights, targets, now))
    if not want_pack and not want_rights:
        return "nothing_to_check" if force else "not_due"
    try:
        async with mtp_store.account_lock(engine, wait_s=lock_wait_s):
            return await _locked(engine, vault, config, targets, want_pack, want_rights, now, options)
    except mtp_store.LockBusy:
        return "busy"


async def _locked(
    engine: AsyncEngine,
    vault: Vault,
    config: operator.OperatorConfig,
    targets: list[Channel],
    want_pack: bool,
    want_rights: bool,
    now: datetime,
    options: ClientOptions | None,
) -> str:
    try:
        async with engine.connect() as conn:
            reason = await operator.account_ready(conn)  # read again under the lock
            if reason is not None:
                return reason
            creds = await mtp_store.load_creds(conn, vault)
            session = await mtp_store.load_session(conn, vault)
    except VaultError:
        return "vault_unavailable"
    if creds is None or session is None:
        return "not_connected"
    public = [t for t in targets if t.username is not None and t.channel_id is not None]
    scope = ChannelScope(
        channel_ids=frozenset(t.channel_id for t in public if t.channel_id is not None),
        usernames=frozenset(t.username for t in public if t.username is not None),
        emoji_set=config.emoji_pack.lower() if want_pack and config.emoji_pack else None,
    )
    found_pack: operator.StoredPack | None = None
    found_rights: dict[str, Any] = {}
    outcome = "ok"
    try:
        async with operator_session(creds, session, options=options, channels=scope) as op:
            if want_pack and config.emoji_pack is not None:
                found_pack = await _read_pack(op, config.emoji_pack, now)
            if want_rights:
                for t in targets:
                    found_rights[t.role] = await _read_rights(op, t)
    except MtpError as exc:
        outcome = f"failed:{exc.code}"
        if want_pack and config.emoji_pack is not None and found_pack is None:
            found_pack = operator.StoredPack(
                short_name=config.emoji_pack, attempted_at=now, error=exc.code, rpc=exc.rpc
            )
        for t in targets:
            if want_rights and t.role not in found_rights:
                found_rights[t.role] = {"chat_id": t.chat_id, "status": "error", "error": exc.code}
    async with engine.begin() as conn:
        # Only for the pack the poster uses NOW: the link (or the channel's own pack, which moves to a
        # newer version by itself, proxyhub/posts/own_pack.py) may have changed while Telegram answered.
        if found_pack is not None and operator.same_pack(
            found_pack.short_name, (await operator.load_config(conn)).emoji_pack
        ):
            await operator.save_pack(conn, _keep_entries(found_pack, await operator.load_pack(conn)))
        if want_rights:
            await operator.save_rights(conn, {"checked_at": iso(now), "channels": found_rights})
    if outcome == "ok" and (
        (found_pack is not None and found_pack.error is not None)
        or any(v.get("status") == "error" for v in found_rights.values())
    ):
        outcome = "partial"
    if outcome != "ok":
        log.warning("operator poster: daily check %s", outcome)
    return outcome


def _keep_entries(new: operator.StoredPack, old: operator.StoredPack | None) -> operator.StoredPack:
    """A failed read of the SAME pack keeps the emoji read last time (and when that was)."""
    if new.error is None or old is None or not operator.same_pack(old.short_name, new.short_name):
        return new
    return operator.StoredPack(
        short_name=new.short_name,
        title=old.title,
        entries=old.entries,
        fetched_at=old.fetched_at,
        attempted_at=new.attempted_at,
        error=new.error,
        rpc=new.rpc,
    )


async def _read_pack(op: Any, short_name: str, now: datetime) -> operator.StoredPack:
    try:
        pack = await op.emoji_pack(short_name)
    except MtpError as exc:
        if exc.code in ("session_terminated", "flood_wait", "telegram_unavailable"):
            raise
        return operator.StoredPack(short_name=short_name, attempted_at=now, error=exc.code, rpc=exc.rpc)
    if not pack.is_emoji:
        return operator.StoredPack(
            short_name=short_name, title=pack.title, attempted_at=now, error="not_emoji_pack"
        )
    return operator.StoredPack(
        short_name=pack.short_name or short_name,
        title=pack.title,
        entries=pack.entries,
        fetched_at=now,
        attempted_at=now,
    )


async def _read_rights(op: Any, t: Channel) -> dict[str, Any]:
    base: dict[str, Any] = {"chat_id": t.chat_id}
    if t.username is None or t.channel_id is None:
        return {**base, "status": "no_username"}
    try:
        ref = await op.resolve_channel(t.username, t.channel_id)
        found = await op.my_rights(ref)
    except MtpError as exc:
        if exc.code in ("session_terminated", "flood_wait", "telegram_unavailable"):
            raise
        status = "channel_not_found" if exc.code == "channel_not_found" else "error"
        return {**base, "status": status, "error": exc.code, "telegram_error": exc.rpc}
    return {
        **base,
        "status": found.status,
        "can_post": found.can_post,
        "can_edit": found.can_edit,
        "can_delete": found.can_delete,
    }

"""The operator account's flows: login (code, optional two-step password), logout and the
worker's session check. Used by proxyhub/api/routes_mtproxy_account.py and
proxyhub/reconcilers/mtproxy_account.py.

Rules kept here:

* One Telegram operation on this account at a time (``store.account_lock``, shared with the worker).
* No network I/O inside an open database transaction: rows are read on a short connection that is
  closed before Telegram is called; results are written in their own short transaction afterwards.
* A second login is refused (409 ``mtproxy_connected``) while a session is stored, until logout.
* Every step is written to actions_log with the phone masked and nothing secret
  (``mtproxy.account_code_sent``, ``..._password_needed``, ``..._connected``, ``..._login_failed``
  with the reason code, ``..._login_cancelled``, ``..._logged_out``, ``..._disconnected``,
  ``..._check_failed``, ``..._check_ok``).
* The login code and the two-step password are used once and never stored.
* This slice sends no message to @MTProxybot or anyone: it only logs in, checks and logs out.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final, NoReturn

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.fleet.actions import log_action
from proxyhub.mtproxy import state, store
from proxyhub.mtproxy.messages import session_ended_alert
from proxyhub.mtproxy.state import AccountRow
from proxyhub.mtproxy.userclient import (
    Account,
    ApiCredentials,
    ClientOptions,
    MtpError,
    OperatorSession,
    operator_session,
)
from proxyhub.mtproxy.validation import mask_phone
from proxyhub.telegram.store import enqueue_owner_alert
from proxyhub.vault import SecretValue, Vault, VaultError, secret_exists

log = logging.getLogger(__name__)

WORKER_ACTOR: Final = "mtproxy"
DEFAULT_LOCK_WAIT_S: Final = 10.0
# A sign-in that fails with one of these ends the login in progress.
_LOGIN_OVER: Final = frozenset(
    {
        "phone_code_expired",
        "session_terminated",
        "phone_number_unoccupied",
        "phone_number_banned",
        "phone_number_invalid",
        "api_id_invalid",
        "api_id_published",
        "update_app_to_login",
    }
)


class ServiceError(Exception):
    """A contract error code (proxyhub/mtproxy/messages.py) with its details. No secret inside."""

    def __init__(
        self,
        code: str,
        *,
        seconds: int | None = None,
        rpc: str | None = None,
        attempts_left: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.seconds = seconds
        self.rpc = rpc
        self.attempts_left = attempts_left

    @classmethod
    def from_mtp(cls, exc: MtpError) -> ServiceError:
        return cls(exc.code, seconds=exc.seconds, rpc=exc.rpc)


@dataclass(frozen=True)
class Deps:
    engine: AsyncEngine
    vault: Vault
    redis: Redis | None
    options: ClientOptions = field(default_factory=ClientOptions)
    lock_wait_s: float = DEFAULT_LOCK_WAIT_S
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


async def _log(
    engine: AsyncEngine,
    *,
    actor: str,
    action: str,
    target: str | None,
    allowed: bool = True,
    reasons: list[str] | None = None,
    context: dict[str, Any] | None = None,
    row: AccountRow | None = None,
) -> None:
    """One short transaction: optionally the new row, always one actions_log line."""
    async with engine.begin() as db:
        if row is not None:
            await store.save_row(db, row)
        await log_action(
            db,
            actor=actor,
            action=action,
            target=target,
            dry_run=False,
            allowed=allowed,
            reasons=reasons or [],
            context=context or {},
        )


async def _login_failed(
    deps: Deps,
    *,
    actor: str,
    target: str | None,
    step: str,
    error: MtpError | ServiceError,
    row: AccountRow | None,
) -> None:
    context: dict[str, Any] = {"step": step}
    if error.seconds is not None:
        context["retry_after_s"] = error.seconds
    if error.rpc is not None:
        context["telegram_error"] = error.rpc
    await _log(
        deps.engine,
        actor=actor,
        action="mtproxy.account_login_failed",
        target=target,
        allowed=False,
        reasons=[error.code],
        context=context,
        row=row,
    )


class _Locked:
    """``async with _Locked(deps):`` the account lock, or ServiceError("mtproxy_busy")."""

    def __init__(self, deps: Deps) -> None:
        self._cm = store.account_lock(deps.engine, wait_s=deps.lock_wait_s)

    async def __aenter__(self) -> None:
        try:
            await self._cm.__aenter__()
        except store.LockBusy:
            raise ServiceError("mtproxy_busy") from None

    async def __aexit__(self, *exc: object) -> None:
        await self._cm.__aexit__(*exc)  # type: ignore[arg-type]


def _redis(deps: Deps) -> Redis:
    if deps.redis is None:
        raise ServiceError("state_unavailable")
    return deps.redis


async def _read(deps: Deps) -> tuple[AccountRow, bool]:
    async with deps.engine.connect() as db:
        return await store.load_row(db), await store.has_session(db)


# --- status ------------------------------------------------------------------------------------------


async def status(engine: AsyncEngine, *, now: datetime) -> dict[str, Any]:
    async with engine.connect() as db:
        row = await store.load_row(db)
        kill_switch = await store.kill_switch_on(db)
    return state.public_status(row, now=now, kill_switch=kill_switch)


# --- login: step 1, send the code ----------------------------------------------------------------------


async def start_login(deps: Deps, *, api_id: int, api_hash: str, phone: str, actor: str) -> AccountRow:
    creds = ApiCredentials(api_id=api_id, api_hash=SecretValue(api_hash))
    phone_secret = SecretValue(phone)
    masked = mask_phone(phone)
    redis = _redis(deps)
    async with _Locked(deps):
        row, session_stored = await _read(deps)
        if session_stored:
            raise ServiceError("mtproxy_connected")
        try:
            await redis.ping()
        except Exception:
            raise ServiceError("state_unavailable") from None
        try:
            async with operator_session(creds, None, phone=phone_secret, options=deps.options) as op:
                sent = await op.send_code(phone_secret)
                session = op.export()
        except MtpError as exc:
            await _login_failed(deps, actor=actor, target=masked, step="send_code", error=exc, row=None)
            raise ServiceError.from_mtp(exc) from None
        login_id = secrets.token_hex(8)
        pending = store.PendingLogin(login_id, creds, phone_secret, session, sent.phone_code_hash)
        try:
            await store.put_pending(redis, deps.vault, pending)
            if row.login_id:
                await store.drop_pending(redis, row.login_id)
        except Exception as exc:
            log.warning("mtproxy: pending login not stored (%s)", type(exc).__name__)
            raise ServiceError("state_unavailable") from None
        new = state.code_sent(
            login_id=login_id, phone_masked=masked, via=sent.via, length=sent.length, now=deps.clock()
        )
        await _log(
            deps.engine,
            actor=actor,
            action="mtproxy.account_code_sent",
            target=masked,
            reasons=[f"code_via:{sent.via}"],
            context={"code_length": sent.length, "expires_at": new.login_expires_at},
            row=new,
        )
        return new


# --- login: steps 2 and 3 ------------------------------------------------------------------------------


async def _pending_for(deps: Deps, *, expected: str, actor: str) -> tuple[AccountRow, store.PendingLogin]:
    """The row and the pending login of the step ``expected``; ends an expired login."""
    redis = _redis(deps)
    row, session_stored = await _read(deps)
    if session_stored:
        raise ServiceError("mtproxy_connected")
    if row.state != expected:
        raise ServiceError("mtproxy_no_login")
    pending = None
    if row.login_id is not None and row.pending_alive(deps.clock()):
        pending = await store.get_pending(redis, deps.vault, row.login_id)
    if pending is None:
        await store.drop_pending(redis, row.login_id)
        expired = ServiceError("mtproxy_login_expired")
        await _login_failed(
            deps, actor=actor, target=row.phone_masked, step=expected, error=expired, row=state.cleared()
        )
        raise expired
    return row, pending


async def _resolve_bot(op: OperatorSession) -> tuple[int | None, str | None]:
    try:
        return await op.resolve_mtproxybot(), None
    except MtpError as exc:
        return None, exc.code


@dataclass(frozen=True)
class _SignedIn:
    account: Account
    session: SecretValue
    bot_id: int | None
    bot_error: str | None


async def _signed_in(op: OperatorSession, account: Account) -> _SignedIn:
    """After a successful sign-in, on the same connection: prove @MTProxybot is visible."""
    bot_id, bot_error = await _resolve_bot(op)
    return _SignedIn(account=account, session=op.export(), bot_id=bot_id, bot_error=bot_error)


async def _finish_login(
    deps: Deps,
    *,
    row: AccountRow,
    pending: store.PendingLogin,
    signed: _SignedIn,
    actor: str,
) -> AccountRow:
    account, session, bot_id, bot_error = signed.account, signed.session, signed.bot_id, signed.bot_error
    new = state.connected(
        row,
        session_id=secrets.token_hex(8),
        user_id=account.user_id,
        first_name=account.first_name,
        mtproxybot_id=bot_id,
        check_error=bot_error,
        now=deps.clock(),
    )
    try:
        async with deps.engine.begin() as db:
            await store.save_account_secrets(db, deps.vault, pending.creds, session)
            await store.save_row(db, new)
            await log_action(
                db,
                actor=actor,
                action="mtproxy.account_connected",
                target=row.phone_masked,
                dry_run=False,
                allowed=True,
                reasons=["mtproxybot_ok" if bot_id is not None else (bot_error or "mtproxybot_not_found")],
                context={"user_id": account.user_id},
            )
    except Exception:
        # Signed in on Telegram but not stored: end that session instead of leaving it orphaned.
        log.warning("mtproxy: the new session could not be stored; ending it on Telegram")
        try:
            async with operator_session(pending.creds, session, options=deps.options) as op:
                await op.log_out()
        except MtpError as exc:
            log.warning("mtproxy: ending the unstored session failed (%s)", exc.code)
        raise
    await store.drop_pending(deps.redis, pending.login_id)
    return new


async def submit_code(deps: Deps, *, code: str, actor: str) -> AccountRow:
    async with _Locked(deps):
        row, pending = await _pending_for(deps, expected=state.CODE_SENT, actor=actor)
        signed: _SignedIn | None = None
        try:
            async with operator_session(
                pending.creds,
                pending.session,
                phone=pending.phone,
                phone_code_hash=pending.phone_code_hash,
                options=deps.options,
            ) as op:
                try:
                    account = await op.sign_in_code(pending.phone, pending.phone_code_hash, SecretValue(code))
                except MtpError as exc:
                    if exc.code != "password_needed":
                        raise
                    pending = replace(pending, session=op.export())
                else:
                    signed = await _signed_in(op, account)
        except MtpError as exc:
            await _sign_in_failed(deps, row=row, pending=pending, exc=exc, step="code", actor=actor)
        if signed is not None:
            return await _finish_login(deps, row=row, pending=pending, signed=signed, actor=actor)
        await store.put_pending(_redis(deps), deps.vault, pending)
        new = state.password_needed(row, deps.clock())
        await _log(
            deps.engine,
            actor=actor,
            action="mtproxy.account_password_needed",
            target=row.phone_masked,
            context={"expires_at": new.login_expires_at},
            row=new,
        )
        return new


async def submit_password(deps: Deps, *, password: str, actor: str) -> AccountRow:
    async with _Locked(deps):
        row, pending = await _pending_for(deps, expected=state.PASSWORD_NEEDED, actor=actor)
        try:
            async with operator_session(
                pending.creds, pending.session, phone=pending.phone, options=deps.options
            ) as op:
                signed = await _signed_in(op, await op.sign_in_password(SecretValue(password)))
        except MtpError as exc:
            await _sign_in_failed(deps, row=row, pending=pending, exc=exc, step="password", actor=actor)
        return await _finish_login(deps, row=row, pending=pending, signed=signed, actor=actor)


async def _sign_in_failed(
    deps: Deps, *, row: AccountRow, pending: store.PendingLogin, exc: MtpError, step: str, actor: str
) -> NoReturn:
    """Record a failed code or password and raise the error the owner sees."""
    target = row.phone_masked
    if step == "code":
        wrong_answer, new, limit = "phone_code_invalid", state.wrong_code(row), state.MAX_WRONG_CODES
        tries = new.wrong_codes
    else:
        wrong_answer, new, limit = "password_invalid", state.wrong_password(row), state.MAX_WRONG_PASSWORDS
        tries = new.wrong_passwords
    if exc.code == wrong_answer:
        if tries >= limit:
            await store.drop_pending(deps.redis, pending.login_id)
            final = ServiceError("too_many_attempts")
            await _login_failed(deps, actor=actor, target=target, step=step, error=final, row=state.cleared())
            raise final
        await _login_failed(deps, actor=actor, target=target, step=step, error=exc, row=new)
        raise ServiceError(exc.code, rpc=exc.rpc, attempts_left=limit - tries)
    if exc.code in _LOGIN_OVER:
        # This login cannot go on (the code or the temporary authorization is gone, or Telegram
        # refuses the number or the app): the owner starts again.
        await store.drop_pending(deps.redis, pending.login_id)
        await _login_failed(deps, actor=actor, target=target, step=step, error=exc, row=state.cleared())
        if exc.code == "session_terminated":
            raise ServiceError("mtproxy_login_expired", rpc=exc.rpc)
        raise ServiceError.from_mtp(exc)
    await _login_failed(deps, actor=actor, target=target, step=step, error=exc, row=None)
    raise ServiceError.from_mtp(exc)


# --- logout --------------------------------------------------------------------------------------------


async def logout(deps: Deps, *, actor: str) -> AccountRow:
    """Ends the session on Telegram (auth.logOut), then removes it and the api credentials from the
    vault. A login in progress is cancelled. Telegram unreachable: nothing is removed (502)."""
    async with _Locked(deps):
        row, session_stored = await _read(deps)
        reason: str
        if session_stored:
            reason = await _end_session_on_telegram(deps)
        elif row.state in state.PENDING or row.login_id:
            await store.drop_pending(deps.redis, row.login_id)
            await _cleared(deps, row, actor=actor, action="mtproxy.account_login_cancelled", reason="owner")
            return state.cleared()
        elif row.state in (state.DISCONNECTED, state.ERROR):
            reason = "session_already_ended"
        else:
            async with deps.engine.connect() as db:
                creds_left = await _secret_left(db)
            if not creds_left:
                raise ServiceError("mtproxy_not_connected")
            reason = "credentials_only"
        await _cleared(deps, row, actor=actor, action="mtproxy.account_logged_out", reason=reason)
        return state.cleared()


async def _secret_left(db: store.Db) -> bool:
    return await secret_exists(db, store.API_SECRET)


async def _end_session_on_telegram(deps: Deps) -> str:
    try:
        async with deps.engine.connect() as db:
            creds = await store.load_creds(db, deps.vault)
            session = await store.load_session(db, deps.vault)
    except VaultError:
        # The stored session cannot be read with this master key: nothing can end it from here.
        return "session_unreadable"
    if creds is None or session is None:
        return "credentials_missing"
    try:
        async with operator_session(creds, session, options=deps.options) as op:
            await op.log_out()
    except MtpError as exc:
        if exc.code == "session_terminated":
            return "session_already_ended"
        raise ServiceError("logout_unavailable", rpc=exc.rpc) from None
    return "telegram_logout"


async def _cleared(deps: Deps, row: AccountRow, *, actor: str, action: str, reason: str) -> None:
    async with deps.engine.begin() as db:
        await store.drop_secrets(db)
        await store.save_row(db, state.cleared())
        await log_action(
            db,
            actor=actor,
            action=action,
            target=row.phone_masked,
            dry_run=False,
            allowed=True,
            reasons=[reason],
            context={"user_id": row.user_id} if row.user_id is not None else {},
        )


# --- the worker's check ----------------------------------------------------------------------------------


async def check_session(deps: Deps) -> str:
    """Every 30 minutes while a session is stored and the kill switch is OFF: connect, get_me,
    disconnect (and resolve @MTProxybot once, if the login could not). Returns what happened:
    ``kill_switch``, ``idle``, ``not_due``, ``ok``, ``terminated``, ``failed:<code>``."""
    now = deps.clock()
    try:
        async with deps.engine.connect() as db:
            if await store.kill_switch_on(db):
                return "kill_switch"  # docs/11 lock 6: the kill switch stops this module too
            row = await store.load_row(db)
            if row.state != state.CONNECTED:
                return "idle"
            if not state.check_due(row, now):
                return "not_due"
            creds = await store.load_creds(db, deps.vault)
            session = await store.load_session(db, deps.vault)
    except VaultError:
        return await _record_check(deps, row, code="session_unreadable", rpc=None, account=None, bot_id=None)
    if creds is None or session is None:
        return await _record_check(deps, row, code="session_missing", rpc=None, account=None, bot_id=None)
    account: Account | None = None
    bot_id = row.mtproxybot_id
    code: str | None = None
    rpc: str | None = None
    try:
        async with operator_session(creds, session, options=deps.options) as op:
            account = await op.get_me()
            if bot_id is None:
                bot_id, _ = await _resolve_bot(op)
    except MtpError as exc:
        code, rpc = exc.code, exc.rpc
    return await _record_check(deps, row, code=code, rpc=rpc, account=account, bot_id=bot_id)


async def _record_check(
    deps: Deps,
    checked: AccountRow,
    *,
    code: str | None,
    rpc: str | None,
    account: Account | None,
    bot_id: int | None,
) -> str:
    now = deps.clock()
    async with deps.engine.begin() as db:
        current = await store.load_row(db, for_update=True)
        if current.session_id != checked.session_id or current.state != state.CONNECTED:
            return "stale"  # logged out or logged in again meanwhile
        new = state.after_check(
            current,
            code=code,
            rpc=rpc,
            first_name=account.first_name if account else None,
            mtproxybot_id=bot_id,
            now=now,
        )
        await store.save_row(db, new)
        target = current.phone_masked
        if code == "session_terminated":
            reason = new.disconnect_reason or "session_terminated"
            await store.drop_secrets(db, session_only=True)
            await log_action(
                db,
                actor=WORKER_ACTOR,
                action="mtproxy.account_disconnected",
                target=target,
                dry_run=False,
                allowed=True,
                reasons=[reason],
                context={"user_id": current.user_id},
            )
            await enqueue_owner_alert(db, session_ended_alert(reason.upper()))
            outcome = "terminated"
        elif code is not None and code not in state.TRANSIENT:
            await log_action(
                db,
                actor=WORKER_ACTOR,
                action="mtproxy.account_check_failed",
                target=target,
                dry_run=False,
                allowed=False,
                reasons=[code],
                context={"telegram_error": rpc} if rpc else {},
            )
            outcome = f"failed:{code}"
        elif code is not None:
            outcome = f"failed:{code}"
        else:
            if current.last_check_error is not None:  # back to normal after a failed check
                await log_action(
                    db,
                    actor=WORKER_ACTOR,
                    action="mtproxy.account_check_ok",
                    target=target,
                    dry_run=False,
                    allowed=True,
                    reasons=["get_me_ok"],
                )
            outcome = "ok"
    if code is not None:
        log.warning("mtproxy: session check failed (%s)", code)
    return outcome

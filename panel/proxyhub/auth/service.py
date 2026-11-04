"""Auth business logic: setup, login, sessions, recovery codes.

Every function takes the request's ``AsyncSession`` and ends its own transaction (commit or
rollback) before returning or raising, so failure counters and audit rows are saved even when the
request is refused. Errors are :class:`AuthError` with a stable code; the HTTP layer maps codes to
status codes and Persian messages.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy import ColumnElement, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.auth import audit, throttle, totp
from proxyhub.auth.passwords import (
    hash_password_async,
    is_valid_username,
    needs_rehash,
    normalize_username,
    password_problem,
    verify_password_async,
)
from proxyhub.auth.secretbox import open_sealed, seal
from proxyhub.auth.tokens import (
    new_recovery_codes,
    new_token,
    recovery_code_hash,
    token_hash,
)
from proxyhub.db.models_auth import (
    AdminEnrollment,
    AdminRecoveryCode,
    AdminSession,
    AdminSetupToken,
    AdminUser,
    AuthThrottle,
)
from proxyhub.reconcilers.base import lock_key

log = logging.getLogger("proxyhub.auth")

SETUP_TOKEN_TTL = timedelta(minutes=60)
SESSION_IDLE_TIMEOUT = timedelta(hours=12)
SESSION_ABSOLUTE_TIMEOUT = timedelta(days=7)
SESSION_TOUCH_INTERVAL = timedelta(seconds=60)
MAX_ACTIVE_SESSIONS = 20
MAX_ENROLLMENTS_PER_TOKEN = 20
SESSION_HISTORY_KEEP = timedelta(days=30)
USER_AGENT_MAX_CHARS = 160
SETUP_LOCK_KEY = lock_key("auth.setup")


class AuthError(Exception):
    """A refused auth request. ``code`` is the API error code; it never carries user input."""

    def __init__(self, code: str, *, retry_after_s: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after_s = retry_after_s


class SetupRefusedError(Exception):
    """A setup link was requested although an admin account already exists."""


@dataclass(frozen=True)
class NewSession:
    token: str  # the cookie value: handed to the browser once, never logged or stored
    session_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class SetupBegin:
    enrollment_id: uuid.UUID
    totp_uri: str
    totp_secret: str


@dataclass(frozen=True)
class SetupFinished:
    username: str
    recovery_codes: list[str]
    session: NewSession


@dataclass(frozen=True)
class LoginOk:
    username: str
    session: NewSession


@dataclass(frozen=True)
class ResolvedSession:
    user_id: uuid.UUID
    username: str
    session_id: uuid.UUID


@dataclass(frozen=True)
class SessionInfo:
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    user_agent: str | None


@dataclass(frozen=True)
class AccountInfo:
    username: str
    created_at: datetime
    last_login_at: datetime | None


def utcnow() -> datetime:
    return datetime.now(UTC)


def _totp_context(user_id: uuid.UUID) -> str:
    return f"admin-totp:{user_id}"


def _enrollment_context(enrollment_id: uuid.UUID) -> str:
    return f"admin-enrollment:{enrollment_id}"


def shorten_user_agent(raw: str | None) -> str | None:
    """Printable characters only, whitespace collapsed, at most 160 characters."""
    if not raw:
        return None
    cleaned = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
    return cleaned[:USER_AGENT_MAX_CHARS] or None


async def _admin_exists(session: AsyncSession) -> bool:
    return (await session.execute(select(AdminUser.id).limit(1))).first() is not None


async def _lock_setup(session: AsyncSession) -> None:
    """Serialise everything that can create the admin account (transaction-scoped lock)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SETUP_LOCK_KEY})


# --- setup ---------------------------------------------------------------------------------


async def setup_required(session: AsyncSession) -> bool:
    required = not await _admin_exists(session)
    await session.commit()
    return required


async def create_setup_token(session: AsyncSession, now: datetime | None = None) -> str:
    """New one-time setup token (only its SHA-256 is stored). Older unused tokens stop working."""
    now = now or utcnow()
    await _lock_setup(session)
    if await _admin_exists(session):
        await session.rollback()
        raise SetupRefusedError
    await session.execute(
        update(AdminSetupToken)
        .where(AdminSetupToken.used_at.is_(None), AdminSetupToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    token = new_token()
    digest = token_hash(token)
    assert digest is not None
    session.add(AdminSetupToken(token_hash=digest, created_at=now, expires_at=now + SETUP_TOKEN_TTL))
    audit.record(
        session, "setup_link_created", reasons={"valid_minutes": int(SETUP_TOKEN_TTL.total_seconds() // 60)}
    )
    await session.commit()
    return token


async def _valid_setup_token(session: AsyncSession, token: str, now: datetime) -> AdminSetupToken | None:
    digest = token_hash(token)
    if digest is None:
        return None
    stmt = (
        select(AdminSetupToken)
        .where(
            AdminSetupToken.token_hash == digest,
            AdminSetupToken.used_at.is_(None),
            AdminSetupToken.revoked_at.is_(None),
            AdminSetupToken.expires_at > now,
        )
        .with_for_update()
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def setup_begin(session: AsyncSession, token: str, now: datetime | None = None) -> SetupBegin:
    """Hand out a fresh TOTP secret for this setup link (each call is a new enrollment)."""
    now = now or utcnow()
    await _lock_setup(session)
    setup_token = await _valid_setup_token(session, token, now)
    if setup_token is None or await _admin_exists(session):
        await session.rollback()
        raise AuthError("setup_token_invalid")
    old = (
        (
            await session.execute(
                select(AdminEnrollment.id)
                .where(AdminEnrollment.setup_token_id == setup_token.id)
                .order_by(AdminEnrollment.created_at.desc(), AdminEnrollment.id)
                .offset(MAX_ENROLLMENTS_PER_TOKEN - 1)
            )
        )
        .scalars()
        .all()
    )
    if old:
        await session.execute(delete(AdminEnrollment).where(AdminEnrollment.id.in_(old)))
    secret = totp.new_secret()
    enrollment_id = uuid.uuid4()
    session.add(
        AdminEnrollment(
            id=enrollment_id,
            setup_token_id=setup_token.id,
            totp_secret_enc=seal(secret.encode(), _enrollment_context(enrollment_id)),
            created_at=now,
        )
    )
    await session.commit()
    return SetupBegin(enrollment_id=enrollment_id, totp_uri=totp.provisioning_uri(secret), totp_secret=secret)


async def setup_finish(
    session: AsyncSession,
    *,
    token: str,
    enrollment_id: str,
    username: str,
    password: str,
    totp_code: str,
    user_agent: str | None,
    now: datetime | None = None,
) -> SetupFinished:
    """Create the admin account, its recovery codes and a first session; the link dies."""
    now = now or utcnow()
    await _lock_setup(session)
    setup_token = await _valid_setup_token(session, token, now)
    if setup_token is None or await _admin_exists(session):
        await session.rollback()
        raise AuthError("setup_token_invalid")
    try:
        enrollment_uuid = uuid.UUID(enrollment_id)
    except ValueError:
        enrollment_uuid = None
    enrollment = None
    if enrollment_uuid is not None:
        enrollment = (
            await session.execute(
                select(AdminEnrollment).where(
                    AdminEnrollment.id == enrollment_uuid, AdminEnrollment.setup_token_id == setup_token.id
                )
            )
        ).scalar_one_or_none()
    if enrollment is None:
        await session.rollback()
        raise AuthError("setup_token_invalid")

    name = normalize_username(username)
    if not is_valid_username(name):
        await session.rollback()
        raise AuthError("invalid_username")
    if password_problem(password, name) is not None:
        await session.rollback()
        raise AuthError("weak_password")
    secret = open_sealed(enrollment.totp_secret_enc, _enrollment_context(enrollment.id)).decode()
    step = totp.match_step(secret, totp_code, None, now)
    if step is None:
        await session.rollback()
        raise AuthError("totp_invalid")

    user_id = uuid.uuid4()
    user = AdminUser(
        id=user_id,
        username=name,
        password_hash=await hash_password_async(password),
        totp_secret_enc=seal(secret.encode(), _totp_context(user_id)),
        totp_last_step=step,
        created_at=now,
        updated_at=now,
        last_login_at=now,
    )
    session.add(user)
    await session.flush()
    codes = _store_new_recovery_codes(session, user_id, now)
    setup_token.used_at = now
    await session.execute(delete(AdminEnrollment).where(AdminEnrollment.setup_token_id == setup_token.id))
    new_session = _create_session(session, user_id, user_agent, now)
    audit.record(session, "setup_completed", target=name)
    await session.commit()
    return SetupFinished(username=name, recovery_codes=codes, session=new_session)


# --- login ---------------------------------------------------------------------------------


def _store_new_recovery_codes(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> list[str]:
    codes = new_recovery_codes()
    for code in codes:
        digest = recovery_code_hash(code)
        assert digest is not None
        session.add(AdminRecoveryCode(user_id=user_id, code_hash=digest, created_at=now))
    return codes


def _create_session(
    session: AsyncSession, user_id: uuid.UUID, user_agent: str | None, now: datetime
) -> NewSession:
    token = new_token()
    digest = token_hash(token)
    assert digest is not None
    row = AdminSession(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=digest,
        created_at=now,
        last_seen_at=now,
        expires_at=now + SESSION_ABSOLUTE_TIMEOUT,
        user_agent=shorten_user_agent(user_agent),
    )
    session.add(row)
    return NewSession(token=token, session_id=row.id, expires_at=row.expires_at)


def _active_session_filter(now: datetime) -> tuple[ColumnElement[bool], ...]:
    return (
        AdminSession.revoked_at.is_(None),
        AdminSession.expires_at > now,
        AdminSession.created_at > now - SESSION_ABSOLUTE_TIMEOUT,
        AdminSession.last_seen_at > now - SESSION_IDLE_TIMEOUT,
    )


async def _tidy_sessions(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    """Keep at most MAX_ACTIVE_SESSIONS live sessions and forget month-old dead ones."""
    live = (
        (
            await session.execute(
                select(AdminSession)
                .where(AdminSession.user_id == user_id, *_active_session_filter(now))
                .order_by(AdminSession.last_seen_at.desc(), AdminSession.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for extra in live[MAX_ACTIVE_SESSIONS:]:
        extra.revoked_at = now
    cutoff = now - SESSION_HISTORY_KEEP
    await session.execute(
        delete(AdminSession).where(
            AdminSession.user_id == user_id,
            (AdminSession.expires_at < cutoff)
            | (AdminSession.revoked_at < cutoff)
            | (AdminSession.last_seen_at < cutoff),
        )
    )


async def login(
    session: AsyncSession,
    *,
    username: str,
    password: str,
    totp_code: str | None,
    recovery_code: str | None,
    user_agent: str | None,
    presented_cookie: str | None = None,
    now: datetime | None = None,
) -> LoginOk:
    """Username + password + (TOTP code or one recovery code). Generic error on any mistake."""
    now = now or utcnow()
    name = normalize_username(username)
    format_ok = is_valid_username(name)
    ukey = throttle.user_key(name) if format_ok else None
    await throttle.ensure_rows(session, [throttle.GLOBAL_KEY] + ([ukey] if ukey else []))
    # Holding the username row for the whole attempt serialises attempts on one username, so
    # parallel requests cannot squeeze in more guesses than the threshold allows.
    user_row = await throttle.load(session, ukey, for_update=True) if ukey else None
    global_row = await throttle.load(session, throttle.GLOBAL_KEY, for_update=False)
    wait = max(throttle.retry_after_s(user_row, now), throttle.retry_after_s(global_row, now))
    if wait:
        await session.rollback()
        raise AuthError("locked", retry_after_s=wait)

    user = None
    if format_ok:
        user = (
            await session.execute(select(AdminUser).where(AdminUser.username == name).with_for_update())
        ).scalar_one_or_none()
    password_ok = await verify_password_async(user.password_hash if user else None, password)

    step: int | None = None
    code_row: AdminRecoveryCode | None = None
    method = "none"
    totp_given = bool(totp_code and totp_code.strip())
    if user is not None:
        if totp_given:
            method = "totp"
            secret = open_sealed(user.totp_secret_enc, _totp_context(user.id)).decode()
            step = totp.match_step(secret, totp_code, user.totp_last_step, now)
        elif recovery_code and recovery_code.strip():
            method = "recovery"
            digest = recovery_code_hash(recovery_code)
            if digest is not None:
                code_row = (
                    await session.execute(
                        select(AdminRecoveryCode)
                        .where(
                            AdminRecoveryCode.user_id == user.id,
                            AdminRecoveryCode.code_hash == digest,
                            AdminRecoveryCode.used_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()

    if user is None or not password_ok or (step is None and code_row is None):
        factor = "unknown_user" if user is None else ("password" if not password_ok else "second_factor")
        target = user.username if user is not None else None
        await _record_failure(
            session, user_row, now, target=target, reasons={"factor": factor, "method": method}
        )

    assert user is not None
    if step is not None:
        user.totp_last_step = step
    if needs_rehash(user.password_hash):
        user.password_hash = await hash_password_async(password)
    user.last_login_at = now
    user.updated_at = now
    if user_row is not None:
        throttle.reset(user_row)
    if presented_cookie:
        old = await _find_session(session, presented_cookie)
        if old is not None and old.user_id == user.id and old.revoked_at is None:
            old.revoked_at = now
    new_session = _create_session(session, user.id, user_agent, now)
    await session.flush()
    await _tidy_sessions(session, user.id, now)
    if code_row is not None:
        code_row.used_at = now
        await session.flush()
        remaining = await _unused_recovery_codes(session, user.id)
        audit.record(session, "recovery_used", target=user.username, reasons={"remaining": remaining})
    audit.record(session, "login_ok", target=user.username, reasons={"method": method})
    await session.commit()
    return LoginOk(username=user.username, session=new_session)


async def _record_failure(
    session: AsyncSession,
    user_row: AuthThrottle | None,
    now: datetime,
    *,
    target: str | None,
    reasons: dict[str, str],
) -> NoReturn:
    """Count the failure (username and global), audit it, commit, and raise the right error."""
    events = []
    if user_row is not None:
        events.append(throttle.register_failure(user_row, throttle.USER_POLICY, now))
    global_row = await throttle.load(session, throttle.GLOBAL_KEY, for_update=True)
    events.append(throttle.register_failure(global_row, throttle.GLOBAL_POLICY, now))
    audit.record(session, "login_failed", target=target, allowed=False, reasons=reasons)
    locks = [e for e in events if e is not None]
    for event in locks:
        audit.record(
            session,
            "locked",
            target=target if event.scope == "user" else None,
            allowed=False,
            reasons={
                "scope": event.scope,
                "level": event.level,
                "lock_s": int(event.duration.total_seconds()),
            },
        )
    await session.commit()
    # Separate transaction: the locks above are released, so pruning cannot deadlock with another
    # attempt that holds its username row and waits for the global row.
    await throttle.prune(session, now)
    await session.commit()
    if locks:
        until = max(e.until for e in locks)
        raise AuthError("locked", retry_after_s=max(1, int((until - now).total_seconds())))
    raise AuthError("invalid_credentials")


async def _unused_recovery_codes(session: AsyncSession, user_id: uuid.UUID) -> int:
    stmt = select(func.count()).where(
        AdminRecoveryCode.user_id == user_id, AdminRecoveryCode.used_at.is_(None)
    )
    return int((await session.execute(stmt)).scalar_one())


# --- sessions ------------------------------------------------------------------------------


async def _find_session(session: AsyncSession, cookie_value: str) -> AdminSession | None:
    digest = token_hash(cookie_value)
    if digest is None:
        return None
    return (
        await session.execute(select(AdminSession).where(AdminSession.token_hash == digest))
    ).scalar_one_or_none()


async def resolve_session(
    session: AsyncSession, cookie_value: str | None, now: datetime | None = None
) -> ResolvedSession | None:
    """The live session behind a cookie, or ``None``. Ends its transaction either way."""
    now = now or utcnow()
    if not cookie_value:
        return None
    digest = token_hash(cookie_value)
    if digest is None:
        return None
    row = (
        await session.execute(
            select(AdminSession, AdminUser.username)
            .join(AdminUser, AdminUser.id == AdminSession.user_id)
            .where(AdminSession.token_hash == digest)
        )
    ).first()
    if row is None:
        await session.rollback()
        return None
    admin_session, username = row
    if (
        admin_session.revoked_at is not None
        or now >= admin_session.expires_at
        or now >= admin_session.created_at + SESSION_ABSOLUTE_TIMEOUT
        or now >= admin_session.last_seen_at + SESSION_IDLE_TIMEOUT
    ):
        await session.rollback()
        return None
    if now - admin_session.last_seen_at >= SESSION_TOUCH_INTERVAL:
        admin_session.last_seen_at = now
    await session.commit()
    return ResolvedSession(user_id=admin_session.user_id, username=username, session_id=admin_session.id)


async def logout(session: AsyncSession, identity: ResolvedSession, now: datetime | None = None) -> None:
    now = now or utcnow()
    await session.execute(
        update(AdminSession)
        .where(AdminSession.id == identity.session_id, AdminSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    audit.record(session, "logout", target=identity.username)
    await session.commit()


async def list_sessions(
    session: AsyncSession, user_id: uuid.UUID, now: datetime | None = None
) -> list[SessionInfo]:
    now = now or utcnow()
    rows = (
        (
            await session.execute(
                select(AdminSession)
                .where(AdminSession.user_id == user_id, *_active_session_filter(now))
                .order_by(AdminSession.last_seen_at.desc(), AdminSession.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return [SessionInfo(r.id, r.created_at, r.last_seen_at, r.user_agent) for r in rows]


async def revoke_session(
    session: AsyncSession, identity: ResolvedSession, session_id: str, now: datetime | None = None
) -> None:
    """End one of the admin's own sessions (the 'sign out' button on the security page)."""
    now = now or utcnow()
    try:
        target_id = uuid.UUID(session_id)
    except ValueError:
        raise AuthError("not_found") from None
    result = await session.execute(
        update(AdminSession)
        .where(
            AdminSession.id == target_id,
            AdminSession.user_id == identity.user_id,
            *_active_session_filter(now),
        )
        .values(revoked_at=now)
        .returning(AdminSession.id)
    )
    if result.first() is None:
        await session.rollback()
        raise AuthError("not_found")
    audit.record(
        session,
        "session_revoked",
        target=f"session:{target_id}",
        reasons={"own_session": target_id == identity.session_id},
    )
    await session.commit()


async def account_info(session: AsyncSession, user_id: uuid.UUID) -> AccountInfo:
    user = (await session.execute(select(AdminUser).where(AdminUser.id == user_id))).scalar_one()
    await session.commit()
    return AccountInfo(username=user.username, created_at=user.created_at, last_login_at=user.last_login_at)


async def regenerate_recovery_codes(
    session: AsyncSession, identity: ResolvedSession, totp_code: str, now: datetime | None = None
) -> list[str]:
    """New set of 10 codes (old ones die). Needs a fresh TOTP code, throttled like a login."""
    now = now or utcnow()
    ukey = throttle.user_key(identity.username)
    await throttle.ensure_rows(session, [ukey])
    user_row = await throttle.load(session, ukey, for_update=True)
    wait = throttle.retry_after_s(user_row, now)
    if wait:
        await session.rollback()
        raise AuthError("locked", retry_after_s=wait)
    user = (
        await session.execute(select(AdminUser).where(AdminUser.id == identity.user_id).with_for_update())
    ).scalar_one()
    secret = open_sealed(user.totp_secret_enc, _totp_context(user.id)).decode()
    step = totp.match_step(secret, totp_code, user.totp_last_step, now)
    if step is None:
        event = throttle.register_failure(user_row, throttle.USER_POLICY, now)
        audit.record(
            session,
            "recovery_regenerate_failed",
            target=user.username,
            allowed=False,
            reasons={"factor": "totp"},
        )
        if event is not None:
            audit.record(
                session,
                "locked",
                target=user.username,
                allowed=False,
                reasons={
                    "scope": event.scope,
                    "level": event.level,
                    "lock_s": int(event.duration.total_seconds()),
                },
            )
        await session.commit()
        if event is not None:
            raise AuthError("locked", retry_after_s=int(event.duration.total_seconds()))
        raise AuthError("totp_invalid")
    user.totp_last_step = step
    user.updated_at = now
    await session.execute(delete(AdminRecoveryCode).where(AdminRecoveryCode.user_id == user.id))
    codes = _store_new_recovery_codes(session, user.id, now)
    audit.record(session, "recovery_codes_regenerated", target=user.username, reasons={"count": len(codes)})
    await session.commit()
    return codes

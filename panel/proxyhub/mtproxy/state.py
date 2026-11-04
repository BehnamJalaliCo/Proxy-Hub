"""The operator account's stored status (settings key ``mtproxy.account``), its transitions and
the API's status shape. Pure: no I/O. Nothing secret is ever part of a row: no phone (only the
masked one), no api_id or api_hash, no code, no password, no session string.

States:

=================  ===============================================================================
``none``           nothing in progress and no session
``code_sent``      Telegram sent a login code; the encrypted pending login waits in Redis (5 minutes)
``password_needed``  the code was right and the account has a two-step password
``connected``      a session is in the vault and the last check (or the login) found it working
``disconnected``   Telegram ended the session (from Settings > Devices, or the account was deleted)
``error``          a session is stored but cannot be used for a reason that does not pass by itself
=================  ===============================================================================

A pending state whose time ran out is shown as ``none`` with ``notice: "login_expired"``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta
from typing import Any, Final

from proxyhub.fleet.actions import iso

NONE: Final = "none"
CODE_SENT: Final = "code_sent"
PASSWORD_NEEDED: Final = "password_needed"  # noqa: S105 - a state name
CONNECTED: Final = "connected"
DISCONNECTED: Final = "disconnected"
ERROR: Final = "error"
STATES: Final = (NONE, CODE_SENT, PASSWORD_NEEDED, CONNECTED, DISCONNECTED, ERROR)
PENDING: Final = (CODE_SENT, PASSWORD_NEEDED)

LOGIN_TTL: Final = timedelta(minutes=5)
CHECK_EVERY: Final = timedelta(minutes=30)
MAX_WRONG_CODES: Final = 5
MAX_WRONG_PASSWORDS: Final = 5
# A check that failed with one of these leaves the state as it was (it passes by itself).
TRANSIENT: Final = frozenset({"telegram_unavailable", "flood_wait"})


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class AccountRow:
    state: str = NONE
    login_id: str | None = None
    phone_masked: str | None = None
    code_via: str | None = None
    code_length: int | None = None
    code_sent_at: str | None = None
    login_expires_at: str | None = None
    wrong_codes: int = 0
    wrong_passwords: int = 0
    session_id: str | None = None
    user_id: int | None = None
    first_name: str | None = None
    mtproxybot_id: int | None = None
    connected_at: str | None = None
    last_check_at: str | None = None
    last_check_error: str | None = None
    disconnected_at: str | None = None
    disconnect_reason: str | None = None
    error: str | None = None

    @classmethod
    def from_json(cls, value: Any) -> AccountRow:
        """A stored row; anything malformed falls back to defaults field by field."""
        if not isinstance(value, dict):
            return cls()
        kept: dict[str, Any] = {}
        for f in fields(cls):
            item = value.get(f.name)
            annotation = str(f.type)  # "str", "int", "str | None", "int | None"
            if item is None:
                ok = "None" in annotation
            elif annotation.startswith("int"):
                ok = isinstance(item, int) and not isinstance(item, bool)
            else:
                ok = isinstance(item, str)
            if ok:
                kept[f.name] = item
        row = cls(**kept)
        return row if row.state in STATES else cls()

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def expires_at(self) -> datetime | None:
        return _parse_time(self.login_expires_at)

    @property
    def last_check(self) -> datetime | None:
        return _parse_time(self.last_check_at)

    def pending_alive(self, now: datetime) -> bool:
        expires = self.expires_at
        return self.state in PENDING and self.login_id is not None and expires is not None and now < expires


# --- transitions ---------------------------------------------------------------------------------


def code_sent(*, login_id: str, phone_masked: str, via: str, length: int | None, now: datetime) -> AccountRow:
    return AccountRow(
        state=CODE_SENT,
        login_id=login_id,
        phone_masked=phone_masked,
        code_via=via,
        code_length=length,
        code_sent_at=iso(now),
        login_expires_at=iso(now + LOGIN_TTL),
    )


def password_needed(row: AccountRow, now: datetime) -> AccountRow:
    return replace(row, state=PASSWORD_NEEDED, login_expires_at=iso(now + LOGIN_TTL))


def wrong_code(row: AccountRow) -> AccountRow:
    return replace(row, wrong_codes=row.wrong_codes + 1)


def wrong_password(row: AccountRow) -> AccountRow:
    return replace(row, wrong_passwords=row.wrong_passwords + 1)


def connected(
    row: AccountRow,
    *,
    session_id: str,
    user_id: int,
    first_name: str,
    mtproxybot_id: int | None,
    check_error: str | None,
    now: datetime,
) -> AccountRow:
    return AccountRow(
        state=CONNECTED,
        phone_masked=row.phone_masked,
        session_id=session_id,
        user_id=user_id,
        first_name=first_name,
        mtproxybot_id=mtproxybot_id,
        connected_at=iso(now),
        last_check_at=iso(now),
        last_check_error=check_error,
    )


def cleared() -> AccountRow:
    """Logged out, login cancelled or abandoned: nothing left."""
    return AccountRow()


def check_due(row: AccountRow, now: datetime, every: timedelta = CHECK_EVERY) -> bool:
    if row.state != CONNECTED or row.session_id is None:
        return False
    last = row.last_check
    return last is None or now - last >= every


def after_check(
    row: AccountRow,
    *,
    code: str | None,
    rpc: str | None = None,
    first_name: str | None = None,
    mtproxybot_id: int | None = None,
    now: datetime,
) -> AccountRow:
    """The row after a check: ``code`` None means get_me answered."""
    if code is None:
        return replace(
            row,
            state=CONNECTED,
            first_name=first_name if first_name is not None else row.first_name,
            mtproxybot_id=mtproxybot_id if mtproxybot_id is not None else row.mtproxybot_id,
            last_check_at=iso(now),
            last_check_error=None,
            error=None,
        )
    if code == "session_terminated":
        return replace(
            row,
            state=DISCONNECTED,
            session_id=None,
            last_check_at=iso(now),
            last_check_error=None,
            disconnected_at=iso(now),
            disconnect_reason=(rpc or "session_terminated").lower(),
        )
    if code in TRANSIENT:
        return replace(row, last_check_at=iso(now), last_check_error=code)
    return replace(row, state=ERROR, last_check_at=iso(now), last_check_error=code, error=code)


# --- the API's view --------------------------------------------------------------------------------


def public_status(row: AccountRow, *, now: datetime, kill_switch: bool) -> dict[str, Any]:
    """``GET /api/mtproxy/account``. Never a secret, never the full phone."""
    state, notice = row.state, None
    if state in PENDING and not row.pending_alive(now):
        state, notice = NONE, "login_expired"
    pending = state in PENDING
    has_account = state in (CONNECTED, DISCONNECTED, ERROR) and row.user_id is not None
    return {
        "state": state,
        "phone_masked": row.phone_masked if state != NONE else None,
        "account": {"first_name": row.first_name or "", "user_id": row.user_id} if has_account else None,
        "login": (
            {
                "code_via": row.code_via,
                "code_length": row.code_length,
                "code_sent_at": row.code_sent_at,
                "expires_at": row.login_expires_at,
            }
            if pending
            else None
        ),
        "mtproxybot_ok": (row.mtproxybot_id is not None) if state in (CONNECTED, ERROR) else None,
        "connected_at": row.connected_at if has_account else None,
        "last_check_at": row.last_check_at if has_account else None,
        "last_check_error": row.last_check_error if has_account else None,
        "disconnected_at": row.disconnected_at if state == DISCONNECTED else None,
        "disconnect_reason": row.disconnect_reason if state == DISCONNECTED else None,
        "error": row.error if state == ERROR else None,
        "notice": notice,
        "kill_switch": kill_switch,
        "check_interval_min": int(CHECK_EVERY.total_seconds() // 60),
    }

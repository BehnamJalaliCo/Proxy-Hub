"""Account operations on the database: add (verify for real), drain, retire, observations, offers.

Rules enforced here:
  3: the token is stored only in the vault (encrypted) and is never logged or returned.
  4: a token whose project holds a server we do not manage is never stored; the account is
     recorded as ``locked`` with a count of foreign servers (no names, no ids of them).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.accounts.hetzner_io import (
    HetznerFailure,
    LiveOffer,
    ServersObservation,
    Verification,
    call_hetzner,
    verify_token,
)
from proxyhub.accounts.state import AccountState, check_account_transition
from proxyhub.db.models import HetznerAccount, Node
from proxyhub.db.models_fleet import AccountObservation, AccountServer, HetznerOffer
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.vault import SecretValue, Vault, delete_secret, hcloud_secret_name

Db = AsyncConnection | AsyncSession

NAME_RE = re.compile(r"^[\w][\w .\-]{0,99}$")
# Hetzner API tokens are 64 alphanumeric characters; accept any printable ASCII without spaces
# so a pasted token with a newline or a header-injection attempt never reaches the API.
TOKEN_RE = re.compile(r"^[\x21-\x7e]{20,256}$")


class AccountError(Exception):
    code: str = "account_error"

    def __init__(self, **extra: Any) -> None:
        super().__init__(self.code)
        self.extra = extra


class NameTaken(AccountError):
    code = "name_taken"


class InvalidName(AccountError):
    code = "invalid_name"


class TokenInvalid(AccountError):
    code = "token_invalid"


class TokenReadOnly(AccountError):
    code = "token_read_only"


class AccountNotEmpty(AccountError):
    code = "account_not_empty"


class HetznerUnavailable(AccountError):
    code = "hetzner_unavailable"


class AccountNotFound(AccountError):
    code = "not_found"


class IllegalTransition(AccountError):
    code = "illegal_transition"


class AccountHasNodes(AccountError):
    code = "account_has_nodes"


# --- reading ----------------------------------------------------------------------------------


def _account_query() -> Any:
    return select(
        HetznerAccount.id,
        HetznerAccount.name,
        HetznerAccount.state,
        HetznerAccount.state_reason,
        HetznerAccount.learned_server_limit,
        HetznerAccount.created_at,
        AccountObservation.servers_total,
    ).outerjoin(AccountObservation, AccountObservation.account_id == HetznerAccount.id)


def _account_json(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "state": row.state,
        "state_reason": row.state_reason,
        "servers_in_account": row.servers_total,
        "learned_server_limit": row.learned_server_limit,
        "created_at": iso(row.created_at),
    }


async def list_accounts(db: Db) -> list[dict[str, Any]]:
    rows = (await db.execute(_account_query().order_by(HetznerAccount.created_at.desc()))).all()
    return [_account_json(r) for r in rows]


async def get_account(db: Db, account_id: uuid.UUID) -> dict[str, Any]:
    row = (await db.execute(_account_query().where(HetznerAccount.id == account_id))).first()
    if row is None:
        raise AccountNotFound()
    return _account_json(row)


# --- observations and offers ------------------------------------------------------------------


async def store_observation(db: Db, account_id: uuid.UUID, obs: ServersObservation, now: datetime) -> None:
    """Record a successful look at the account. Only OUR servers are stored by name."""
    values = {
        "account_id": account_id,
        "checked_at": now,
        "ok": True,
        "guard_ok": obs.guard.allowed,
        "servers_total": obs.total,
        "managed_servers": len(obs.managed),
        "foreign_servers": obs.foreign_count,
        "error": None,
        "last_ok_at": now,
    }
    stmt = insert(AccountObservation).values(**values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[AccountObservation.account_id],
            set_={k: stmt.excluded[k] for k in values if k != "account_id"},
        )
    )
    await db.execute(delete(AccountServer).where(AccountServer.account_id == account_id))
    if obs.managed:
        await db.execute(
            insert(AccountServer),
            [
                {
                    "account_id": account_id,
                    "hetzner_id": s.hetzner_id,
                    "name": s.name[:100],
                    "labels": dict(s.labels),
                    "delete_protected": s.delete_protected,
                    "seen_at": now,
                }
                for s in obs.managed
            ],
        )


async def store_observation_failure(db: Db, account_id: uuid.UUID, error_code: str, now: datetime) -> None:
    """Record a failed look; the counters of the last good look stay, ``ok`` becomes false."""
    stmt = insert(AccountObservation).values(
        account_id=account_id, checked_at=now, ok=False, error=error_code[:60]
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[AccountObservation.account_id],
            set_={"checked_at": stmt.excluded.checked_at, "ok": False, "error": stmt.excluded.error},
        )
    )


@dataclass(frozen=True)
class PriceChange:
    server_type: str
    location: str
    old_monthly: Decimal | None
    new_monthly: Decimal | None


async def store_offers(
    db: Db, account_id: uuid.UUID, offers: Sequence[LiveOffer], now: datetime
) -> list[PriceChange]:
    """Replace the account's live offers; returns monthly price changes (new, changed, gone)."""
    old = {
        (r.server_type, r.location): Decimal(r.price_monthly_net)
        for r in (
            await db.execute(
                select(HetznerOffer.server_type, HetznerOffer.location, HetznerOffer.price_monthly_net).where(
                    HetznerOffer.account_id == account_id
                )
            )
        ).all()
    }
    await db.execute(delete(HetznerOffer).where(HetznerOffer.account_id == account_id))
    new: dict[tuple[str, str], Decimal] = {}
    rows = []
    for o in offers:
        key = (o.server_type, o.location)
        if key in new:
            continue
        new[key] = o.price.monthly
        rows.append(
            {
                "account_id": account_id,
                "server_type": o.server_type[:40],
                "location": o.location[:20],
                "price_hourly_net": o.price.hourly,
                "price_monthly_net": o.price.monthly,
                "included_traffic_bytes": o.included_traffic_bytes,
                "deprecated": o.deprecated,
                "available": o.available,
                "fetched_at": now,
            }
        )
    if rows:
        await db.execute(insert(HetznerOffer), rows)
    changes = [
        PriceChange(t, loc, old.get((t, loc)), new.get((t, loc)))
        for (t, loc) in sorted(set(old) | set(new))
        if old.get((t, loc)) != new.get((t, loc))
    ]
    return changes


# --- state changes ----------------------------------------------------------------------------


async def transition_account(
    db: Db, account_id: uuid.UUID, target: AccountState, reason: str, *, actor: str
) -> str:
    """Move an account through the state machine under a row lock. Returns the previous state."""
    current = (
        await db.execute(
            select(HetznerAccount.state).where(HetznerAccount.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if current is None:
        raise AccountNotFound()
    try:
        check_account_transition(current, target)
    except ValueError:
        raise IllegalTransition(state=current, target=target.value) from None
    await db.execute(
        text("UPDATE hetzner_accounts SET state = :s, state_reason = :r, updated_at = now() WHERE id = :id"),
        {"s": target.value, "r": reason, "id": account_id},
    )
    await log_action(
        db,
        actor=actor,
        action=f"account_{target.value}",
        target=str(account_id),
        dry_run=False,
        allowed=True,
        reasons=[reason],
        context={"from": current, "to": target.value},
        result=f"{current} -> {target.value}",
    )
    return str(current)


async def drain_account(db: Db, account_id: uuid.UUID, *, actor: str) -> None:
    await transition_account(db, account_id, AccountState.DRAINING, "drained_by_owner", actor=actor)


async def live_nodes_in_account(db: Db, account_id: uuid.UUID) -> int:
    n = (
        await db.execute(
            select(func.count())
            .select_from(Node)
            .where(Node.account_id == account_id, Node.state != NodeState.DELETED.value)
        )
    ).scalar_one()
    return int(n)


async def retire_account(db: Db, account_id: uuid.UUID, *, actor: str) -> None:
    """draining|locked -> retired, only with no live node of ours there; the token leaves the vault."""
    state = (
        await db.execute(
            select(HetznerAccount.state).where(HetznerAccount.id == account_id).with_for_update()
        )
    ).scalar_one_or_none()
    if state is None:
        raise AccountNotFound()
    try:
        check_account_transition(state, AccountState.RETIRED)
    except ValueError:
        raise IllegalTransition(state=state, target=AccountState.RETIRED.value) from None
    live = await live_nodes_in_account(db, account_id)
    if live:
        raise AccountHasNodes(live_nodes=live)
    await transition_account(db, account_id, AccountState.RETIRED, "retired_by_owner", actor=actor)
    await delete_secret(db, hcloud_secret_name(account_id))


# --- adding an account ------------------------------------------------------------------------


async def verify_with_hetzner(token: SecretValue) -> Verification:
    return await call_hetzner(lambda: verify_token(token.reveal()))


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not NAME_RE.fullmatch(name):
        raise InvalidName()
    return name


def clean_token(token: str) -> SecretValue:
    token = token.strip()
    if not TOKEN_RE.fullmatch(token):
        raise TokenInvalid()
    return SecretValue(token)


async def _name_taken(db: Db, name: str) -> bool:
    row = (
        await db.execute(select(HetznerAccount.id).where(func.lower(HetznerAccount.name) == name.lower()))
    ).first()
    return row is not None


async def add_account(
    session: AsyncSession,
    vault: Vault,
    *,
    name: str,
    token: SecretValue,
    actor: str,
    now: datetime,
) -> dict[str, Any]:
    """Verify a token against the real Hetzner API and store the account.

    The session is committed or rolled back here, and no transaction is held open while Hetzner
    is being asked.
    """
    name = clean_name(name)
    if await _name_taken(session, name):
        raise NameTaken()
    await session.rollback()  # do not hold a transaction open across the network calls

    try:
        result = await verify_with_hetzner(token)
    except HetznerFailure as exc:
        if exc.kind in ("token_invalid", "forbidden"):
            raise TokenInvalid() from None
        if exc.kind == "read_only":
            raise TokenReadOnly() from None
        raise HetznerUnavailable(hetzner_code=exc.code) from None
    return await persist_verified_account(
        session, vault, name=name, token=token, result=result, actor=actor, now=now
    )


async def persist_verified_account(
    session: AsyncSession,
    vault: Vault,
    *,
    name: str,
    token: SecretValue,
    result: Verification,
    actor: str,
    now: datetime,
) -> dict[str, Any]:
    """Store the outcome of a real verification (state machine verifying -> active | locked)."""
    name = clean_name(name)
    if await _name_taken(session, name):
        raise NameTaken()
    if result.servers.guard.allowed:
        if result.access == "invalid":
            raise TokenInvalid()
        if result.access == "read_only":
            raise TokenReadOnly()
        if result.access != "read_write":
            raise HetznerUnavailable(hetzner_code="write_probe_inconclusive")

    account_id = uuid.uuid4()
    try:
        session.add(
            HetznerAccount(
                id=account_id,
                name=name,
                token_secret_name=hcloud_secret_name(account_id),
                state=AccountState.VERIFYING.value,
            )
        )
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise NameTaken() from None

    obs = result.servers
    if not obs.guard.allowed:
        # Rule 4: never keep a token that can see servers we do not manage. Record the account
        # as locked (counts only) so the owner sees what happened, and stop.
        await transition_account(
            session, account_id, AccountState.LOCKED, f"foreign_servers:{obs.foreign_count}", actor=actor
        )
        await store_observation(session, account_id, obs, now)
        await log_action(
            session,
            actor=actor,
            action="account_add",
            target=name,
            dry_run=False,
            allowed=False,
            reasons=["account_not_empty"],
            context={"account_id": str(account_id), "foreign_servers": obs.foreign_count},
            result="token rejected and not stored",
        )
        await session.commit()
        raise AccountNotEmpty(foreign_servers=obs.foreign_count)

    await vault.put(session, hcloud_secret_name(account_id), token)
    await store_observation(session, account_id, obs, now)
    await store_offers(session, account_id, result.offers, now)
    await transition_account(session, account_id, AccountState.ACTIVE, "verified", actor=actor)
    await log_action(
        session,
        actor=actor,
        action="account_add",
        target=name,
        dry_run=False,
        allowed=True,
        reasons=["verified"],
        context={
            "account_id": str(account_id),
            "servers_in_account": obs.total,
            "managed_servers": len(obs.managed),
            "offers": len(result.offers),
        },
        result="account active",
    )
    await session.commit()
    return await get_account(session, account_id)

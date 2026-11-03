"""Account guard loop (CLAUDE.md rule 4; docs/10_IMPLEMENTATION_PLAN.md section 3.3).

Every 10 minutes per account (and once right after each worker start) it lists ALL servers of the
account's project, without a label filter. A single server without ``managed-by=proxy-hub`` locks
the account; so does a token Hetzner no longer accepts (401/403). A locked account is never
unlocked by code: the owner decides. Transient failures (network, rate limit, 5xx) only mark the
observation as failed; the planner treats an observation older than 30 minutes as "guard not green".
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.accounts.hetzner_io import HetznerFailure, ServersObservation, call_hetzner, observe_servers
from proxyhub.accounts.service import store_observation, store_observation_failure, transition_account
from proxyhub.accounts.state import OBSERVED_STATES, AccountState
from proxyhub.db.models import HetznerAccount
from proxyhub.db.models_fleet import AccountObservation
from proxyhub.fleet.actions import log_action
from proxyhub.hetzner.client import HetznerReader
from proxyhub.reconcilers.base import Reconciler
from proxyhub.vault import Vault, hcloud_secret_name

log = logging.getLogger(__name__)

GUARD_INTERVAL = timedelta(minutes=10)
ACTOR = "account_guard"


@dataclass(frozen=True)
class AccountRef:
    id: uuid.UUID
    name: str
    state: str
    checked_at: datetime | None


async def observed_accounts(conn: AsyncConnection) -> list[AccountRef]:
    rows = (
        await conn.execute(
            select(
                HetznerAccount.id, HetznerAccount.name, HetznerAccount.state, AccountObservation.checked_at
            )
            .outerjoin(AccountObservation, AccountObservation.account_id == HetznerAccount.id)
            .where(HetznerAccount.state.in_([s.value for s in OBSERVED_STATES]))
            .order_by(AccountObservation.checked_at.asc().nulls_first())
        )
    ).all()
    return [AccountRef(r.id, r.name, r.state, r.checked_at) for r in rows]


async def apply_observation(
    conn: AsyncConnection, account: AccountRef, obs: ServersObservation, now: datetime
) -> bool:
    """Store a successful observation; lock the account if the guard fails. Returns guard_ok."""
    await store_observation(conn, account.id, obs, now)
    if obs.guard.allowed:
        return True
    await transition_account(
        conn, account.id, AccountState.LOCKED, f"foreign_servers:{obs.foreign_count}", actor=ACTOR
    )
    await log_action(
        conn,
        actor=ACTOR,
        action="account_guard",
        target=account.name,
        dry_run=False,
        allowed=False,
        reasons=["foreign_servers"],
        context={"account_id": str(account.id), "foreign_servers": obs.foreign_count},
        result="account locked: a server we do not manage is in this project",
    )
    return False


async def apply_failure(
    conn: AsyncConnection, account: AccountRef, failure: HetznerFailure, now: datetime
) -> None:
    await store_observation_failure(conn, account.id, f"{failure.kind}:{failure.code}", now)
    reason = {"token_invalid": "token_rejected", "forbidden": "token_forbidden"}.get(failure.kind)
    if reason is None:
        return
    await transition_account(conn, account.id, AccountState.LOCKED, reason, actor=ACTOR)
    await log_action(
        conn,
        actor=ACTOR,
        action="account_guard",
        target=account.name,
        dry_run=False,
        allowed=False,
        reasons=[reason],
        context={"account_id": str(account.id), "hetzner_code": failure.code},
        result="account locked: Hetzner refused the token",
    )


class AccountGuardReconciler(Reconciler):
    name = "account_guard"
    interval_seconds = 60.0

    def __init__(
        self,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        reader_factory: Callable[[str], HetznerReader] = HetznerReader,
    ) -> None:
        self._vault_factory = vault_factory
        self._reader_factory = reader_factory
        self._checked_since_start: set[uuid.UUID] = set()

    def is_due(self, account: AccountRef, now: datetime) -> bool:
        if account.id not in self._checked_since_start:
            return True
        return account.checked_at is None or now - account.checked_at >= GUARD_INTERVAL

    async def run_once(self, conn: AsyncConnection) -> None:
        now = datetime.now(UTC)
        due = [a for a in await observed_accounts(conn) if self.is_due(a, now)]
        if not due:
            return
        vault = self._vault_factory()
        for account in due:
            await self.check(conn, vault, account, now)

    async def check(self, conn: AsyncConnection, vault: Vault, account: AccountRef, now: datetime) -> None:
        self._checked_since_start.add(account.id)
        token = await vault.get(conn, hcloud_secret_name(account.id))
        if token is None:
            await store_observation_failure(conn, account.id, "token_missing", now)
            log.warning("account %s has no token in the vault", account.name)
            return
        reader_factory = self._reader_factory
        try:
            obs = await call_hetzner(lambda: observe_servers(reader_factory(token.reveal())))
        except HetznerFailure as failure:
            log.warning("account guard for %s failed: %s", account.name, failure)
            await apply_failure(conn, account, failure, now)
            return
        await apply_observation(conn, account, obs, now)

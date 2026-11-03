"""Account state machine (docs/10_IMPLEMENTATION_PLAN.md section 3.8). Unlisted transitions are refused.

verifying -> active | locked       adding a token: guard green -> active; foreign servers -> locked
verifying -> preparing -> active   (preparing: firewall + image per account, milestone M2.4)
active    -> draining | locked     owner drains; account_guard locks on a foreign server or 401/403
draining  -> retired | locked      retired only when none of our nodes is live there
locked    -> retired               owner gives the account up; a locked account is never unlocked by code
"""

from __future__ import annotations

from enum import StrEnum

from proxyhub.db.models import ACCOUNT_STATES


class AccountState(StrEnum):
    VERIFYING = "verifying"
    PREPARING = "preparing"
    ACTIVE = "active"
    DRAINING = "draining"
    RETIRED = "retired"
    LOCKED = "locked"


A = AccountState

ACCOUNT_TRANSITIONS: dict[AccountState, frozenset[AccountState]] = {
    A.VERIFYING: frozenset({A.ACTIVE, A.PREPARING, A.LOCKED}),
    A.PREPARING: frozenset({A.ACTIVE, A.LOCKED}),
    A.ACTIVE: frozenset({A.DRAINING, A.LOCKED}),
    A.DRAINING: frozenset({A.RETIRED, A.LOCKED}),
    A.LOCKED: frozenset({A.RETIRED}),
    A.RETIRED: frozenset(),
}

# Accounts whose token the panel still uses to read Hetzner (guard and pricing loops).
OBSERVED_STATES: frozenset[AccountState] = frozenset({A.VERIFYING, A.PREPARING, A.ACTIVE, A.DRAINING})

assert {s.value for s in AccountState} == set(ACCOUNT_STATES), "keep in sync with the DB check constraint"


class IllegalAccountTransition(ValueError):
    pass


def check_account_transition(current: str, target: AccountState) -> AccountState:
    cur = AccountState(current)
    if target not in ACCOUNT_TRANSITIONS[cur]:
        raise IllegalAccountTransition(f"{cur} -> {target} is not allowed")
    return target


# Machine codes stored in hetzner_accounts.state_reason, with the Persian text the dashboard shows.
REASON_FA: dict[str, str] = {
    "verified": "توکن وارسی شد و project فقط سرورهای این سیستم را دارد.",
    "foreign_servers": "در این project سروری هست که مال این سیستم نیست؛ حساب قفل شد (قانون ۴).",
    "token_rejected": "هتزنر توکن این حساب را دیگر نمی‌پذیرد (401)؛ حساب قفل شد.",
    "token_forbidden": "هتزنر دسترسی این توکن را رد کرد (403)؛ حساب قفل شد.",
    "drained_by_owner": "مالک حساب را تخلیه کرد؛ node تازه در آن ساخته نمی‌شود.",
    "retired_by_owner": "حساب بازنشسته شد و توکنش از گاوصندوق پاک شد.",
}


def reason_code(state_reason: str | None) -> str | None:
    """'foreign_servers:2' -> 'foreign_servers'."""
    return state_reason.split(":", 1)[0] if state_reason else None

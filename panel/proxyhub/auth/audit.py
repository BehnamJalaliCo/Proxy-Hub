"""Audit trail of auth events in ``actions_log`` (actor 'auth'). No secrets, no IPs, ever:
targets are usernames or session ids, reasons are small enums and counts."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.db.models import ActionLog

ACTOR = "auth"
log = logging.getLogger("proxyhub.auth")


def record(
    session: AsyncSession,
    action: str,
    *,
    target: str | None = None,
    allowed: bool = True,
    reasons: dict[str, Any] | None = None,
    result: str | None = None,
) -> None:
    """Add one row to the caller's transaction (committed together with the change it describes)."""
    session.add(
        ActionLog(
            actor=ACTOR,
            action=action,
            target=target,
            dry_run=False,
            allowed=allowed,
            reasons=reasons or {},
            result=result,
        )
    )
    log.info("auth event %s target=%s allowed=%s", action, target or "-", allowed)

"""actions_log writer and reader helpers (the dashboard's "events").

Row convention used by this team: ``reasons`` JSONB is ``{"reasons": [codes...], "context": {...}}``.
Rows written by other code may hold another JSON shape; :func:`reason_list` normalises any of them
into the list of strings the API contract promises. Nothing secret ever goes into a row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models import ActionLog


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 in UTC with a trailing Z (API contract)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def log_action(
    db: AsyncConnection | AsyncSession,
    *,
    actor: str,
    action: str,
    target: str | None,
    dry_run: bool,
    allowed: bool,
    reasons: Sequence[str] = (),
    context: Mapping[str, Any] | None = None,
    result: str | None = None,
) -> int:
    row = (
        await db.execute(
            insert(ActionLog)
            .values(
                actor=actor[:60],
                action=action[:60],
                target=target[:200] if target is not None else None,
                dry_run=dry_run,
                allowed=allowed,
                reasons={"reasons": [str(r) for r in reasons], "context": dict(context or {})},
                result=result,
            )
            .returning(ActionLog.id)
        )
    ).scalar_one()
    return int(row)


def reason_list(value: Any) -> list[str]:
    """Any stored ``reasons`` JSON -> list of reason strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    if isinstance(value, Mapping):
        inner = value.get("reasons")
        if isinstance(inner, list | tuple):
            return [str(v) for v in inner]
        return [f"{k}:{v}" if v not in (None, True) else str(k) for k, v in value.items()]
    return [str(value)]

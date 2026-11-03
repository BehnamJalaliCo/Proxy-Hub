"""Learn an account's server limit from Hetzner's ``resource_limit_exceeded`` error (finding H7).

Hetzner does not publish a new account's default server limit, and the API has no endpoint for
it. When a create call hits the limit the API answers HTTP 403 with this body (docs.hetzner.cloud,
"Errors"):

    {"error": {"code": "resource_limit_exceeded",
               "message": "project limit exceeded",
               "details": {"limits": [{"name": "project_limit"}]}}}

The limit's value is normally *not* in the error. So the limit is learned as "the number of
servers that were in the account when the create was refused", unless the message states a
number of servers explicitly. A failed create in an account means that account cannot take
another server right now whatever resource ran out (servers, cores, primary IPs), so every
``resource_limit_exceeded`` raised by a server create teaches a server limit.

The exact wording of real server-limit messages is checked on the panel server (F8); the parser
is deliberately tolerant and is tested on the documented shape and on plausible variants.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from hcloud import APIException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.fleet.actions import log_action

RESOURCE_LIMIT_CODE = "resource_limit_exceeded"

LimitKind = Literal["server", "core", "primary_ip", "project", "other"]

_KIND_PATTERNS: tuple[tuple[LimitKind, re.Pattern[str]], ...] = (
    ("primary_ip", re.compile(r"primary[\s_-]?ips?", re.IGNORECASE)),
    ("core", re.compile(r"\b(cores?|vcpus?|cpus?)\b", re.IGNORECASE)),
    ("server", re.compile(r"\bservers?\b", re.IGNORECASE)),
    ("project", re.compile(r"\bproject\b", re.IGNORECASE)),
)
_STATED_SERVER_LIMIT = re.compile(
    r"(?:limit\s+(?:of\s+)?(\d+)\s+servers?)|(?:(\d+)\s+servers?\s+(?:limit|allowed|maximum))|"
    r"(?:server\s+limit\s+(?:of\s+|is\s+)?(\d+))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResourceLimit:
    kind: LimitKind
    limit_names: tuple[str, ...]
    stated_server_limit: int | None


def _error_parts(error: APIException | Mapping[str, Any]) -> tuple[str, str, Any]:
    if isinstance(error, APIException):
        return str(error.code), str(error.message or ""), error.details
    body = error.get("error", error)
    if not isinstance(body, Mapping):
        return "", "", None
    return str(body.get("code", "")), str(body.get("message", "") or ""), body.get("details")


def parse_resource_limit(error: APIException | Mapping[str, Any]) -> ResourceLimit | None:
    """Parse an ``hcloud.APIException`` or a raw Hetzner error body; None if it is another error."""
    code, message, details = _error_parts(error)
    if code != RESOURCE_LIMIT_CODE:
        return None
    names: list[str] = []
    if isinstance(details, Mapping):
        for item in details.get("limits") or []:
            if isinstance(item, Mapping) and item.get("name"):
                names.append(str(item["name"]))
    haystack = " ".join([*names, message]).replace("_", " ")
    kind: LimitKind = "other"
    for candidate, pattern in _KIND_PATTERNS:
        if pattern.search(haystack):
            kind = candidate
            break
    stated: int | None = None
    if kind == "server":
        m = _STATED_SERVER_LIMIT.search(message)
        if m:
            stated = int(next(g for g in m.groups() if g is not None))
    return ResourceLimit(kind=kind, limit_names=tuple(names), stated_server_limit=stated)


def learned_server_limit(limit: ResourceLimit, servers_in_account: int) -> int:
    """The server limit to remember for the account after a create was refused with ``limit``."""
    if servers_in_account < 0:
        raise ValueError("servers_in_account must be >= 0")
    if limit.stated_server_limit is not None:
        return limit.stated_server_limit
    return servers_in_account


async def record_resource_limit(
    db: AsyncConnection | AsyncSession,
    account_id: uuid.UUID,
    servers_in_account: int,
    error: APIException | Mapping[str, Any],
    *,
    actor: str = "fleet",
) -> int | None:
    """Store the learned limit on the account and in actions_log. Returns it, or None if not a limit error."""
    parsed = parse_resource_limit(error)
    if parsed is None:
        return None
    learned = learned_server_limit(parsed, servers_in_account)
    await db.execute(
        text("UPDATE hetzner_accounts SET learned_server_limit = :n, updated_at = now() WHERE id = :id"),
        {"n": learned, "id": account_id},
    )
    await log_action(
        db,
        actor=actor,
        action="learned_server_limit",
        target=str(account_id),
        dry_run=False,
        allowed=True,
        reasons=["resource_limit_exceeded"],
        context={
            "kind": parsed.kind,
            "limit_names": list(parsed.limit_names),
            "servers_in_account": servers_in_account,
            "learned_server_limit": learned,
        },
        result=f"learned server limit {learned}",
    )
    return learned

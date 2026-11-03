"""One-time node registration tokens (docs/10 section 3.5).

The fleet calls :func:`issue_registration_token` right before it creates a server and puts the
returned token into that server's cloud-init. Only the SHA-256 of the token is stored; the token is
valid for one hour and for exactly one successful registration. Issuing a new token for a node
cancels any earlier unused one.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.db.models import Node
from proxyhub.db.models_agent import AgentRegistrationToken
from proxyhub.fleet.lifecycle import NodeState

TOKEN_TTL = timedelta(hours=1)
# 32 random bytes, URL-safe base64 without padding: 43 characters.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# A node in one of these states must never get (or use) a registration token.
CLOSED_STATES = frozenset({NodeState.RETIRING.value, NodeState.DELETED.value})


class TokenRefused(RuntimeError):
    """A token can not be issued for this node (unknown node, or retiring/deleted)."""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def issue_registration_token(
    session: AsyncSession, node_id: uuid.UUID, *, now: datetime | None = None, ttl: timedelta = TOKEN_TTL
) -> str:
    """Create a one-time token for ``node_id`` and return it (the only time it exists in clear).

    The caller commits. The returned value must go only into the node's cloud-init user data.
    """
    moment = now or datetime.now(UTC)
    state = (await session.execute(select(Node.state).where(Node.id == node_id))).scalar_one_or_none()
    if state is None or state in CLOSED_STATES:
        raise TokenRefused("node unknown or closed")
    await session.execute(
        update(AgentRegistrationToken)
        .where(
            AgentRegistrationToken.node_id == node_id,
            AgentRegistrationToken.used_at.is_(None),
            AgentRegistrationToken.expires_at > moment,
        )
        .values(expires_at=moment)
        .execution_options(synchronize_session=False)
    )
    token = secrets.token_urlsafe(32)
    session.add(
        AgentRegistrationToken(
            node_id=node_id,
            token_sha256=token_digest(token),
            created_at=moment,
            expires_at=moment + ttl,
        )
    )
    await session.flush()
    return token


async def consume_registration_token(session: AsyncSession, token: str, *, now: datetime) -> uuid.UUID | None:
    """Atomically mark a valid token used and return its node id; ``None`` if the token is unknown,
    expired or already used. One UPDATE ... RETURNING, so two racing registrations can not both win.
    The caller commits (or rolls back, which leaves the token unused)."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    result = await session.execute(
        update(AgentRegistrationToken)
        .where(
            AgentRegistrationToken.token_sha256 == token_digest(token),
            AgentRegistrationToken.used_at.is_(None),
            AgentRegistrationToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(AgentRegistrationToken.node_id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none()

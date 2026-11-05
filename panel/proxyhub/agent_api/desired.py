"""Desired Telemt state of a node, and the service functions that change it.

The document served at ``GET /agent/v1/desired`` is plain JSON in a canonical form (sorted keys,
no spaces), and its ETag is the SHA-256 of exactly those bytes: it changes when, and only when,
something the node must apply changes. It carries Telemt user secrets and, in its ``xray`` section,
the scope's REALITY private key or (transport ws_tls) the TLS certificate's private key for the
node's own name, so it is only ever sent to the authenticated agent of that node and is never
logged. The same builder serves the agent and the MTProto live check (one document, one ETag).

Privacy and sponsorship invariants are constants here, not data: ``use_middle_proxy = true``
(needed for the sponsor channel, decision D26), ``beobachten = false`` and ``log_level = "silent"``
(rule 7). The agent refuses any document or rendered config that says otherwise.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.agent_api.sealing import open_secret, seal_secret
from proxyhub.agent_api.validation import HEX32, RESERVED_PORTS, valid_domain, valid_user_name
from proxyhub.db.models_agent import TelemtNodeConfig, TelemtUser
from proxyhub.reality.store import xray_section

DESIRED_FORMAT = 1
POLL_INTERVAL_S = 15
REPORT_INTERVAL_S = 60
MAX_USERS_PER_NODE = 1000
# Fixed by policy (decision D26, CLAUDE.md rule 7); never configurable.
USE_MIDDLE_PROXY = True
BEOBACHTEN = False
LOG_LEVEL = "silent"


class DesiredStateError(ValueError):
    """A requested Telemt setting is invalid (message is safe to show; it never holds a secret)."""


def canonical_json(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def etag_for(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest() + '"'


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_ad_tag(ad_tag: str | None) -> None:
    if ad_tag is not None and not HEX32.fullmatch(ad_tag):
        raise DesiredStateError("ad_tag must be 32 lower-case hex characters")


async def build_desired_state(session: AsyncSession, seal_key: bytes, node_id: uuid.UUID) -> dict[str, Any]:
    config = await session.get(TelemtNodeConfig, node_id)
    doc: dict[str, Any] = {
        "format": DESIRED_FORMAT,
        "node_id": str(node_id),
        "intervals": {"poll_s": POLL_INTERVAL_S, "report_s": REPORT_INTERVAL_S},
        "telemt": None,
    }
    xray = await xray_section(session, node_id)  # VLESS (proxyhub/reality): ws_tls or reality; None: left out
    if xray is not None:
        doc["xray"] = xray
    if config is None:
        return doc
    users = (
        (
            await session.execute(
                select(TelemtUser).where(TelemtUser.node_id == node_id).order_by(TelemtUser.name)
            )
        )
        .scalars()
        .all()
    )
    doc["telemt"] = {
        "ports": list(config.ports),
        "tls_domain": config.tls_domain,
        "modes": {"classic": config.mode_classic, "secure": config.mode_secure, "tls": config.mode_tls},
        "ad_tag": config.ad_tag,
        "use_middle_proxy": USE_MIDDLE_PROXY,
        "beobachten": BEOBACHTEN,
        "log_level": LOG_LEVEL,
        "users": [
            {
                "name": user.name,
                "secret": open_secret(seal_key, node_id, user.name, user.secret_sealed).hex(),
                "enabled": user.enabled,
                "ad_tag": user.ad_tag,
                "expires_at": _utc_text(user.expires_at) if user.expires_at else None,
                "data_quota_bytes": user.data_quota_bytes,
                "max_tcp_conns": user.max_tcp_conns,
            }
            for user in users
        ],
    }
    return doc


async def set_telemt_node_config(
    session: AsyncSession,
    node_id: uuid.UUID,
    *,
    ports: list[int],
    tls_domain: str,
    ad_tag: str | None = None,
    classic: bool = False,
    secure: bool = False,
    tls: bool = True,
) -> None:
    """Create or replace a node's Telemt settings (the caller commits)."""
    if not 1 <= len(ports) <= 4 or len(set(ports)) != len(ports):
        raise DesiredStateError("ports: 1 to 4 distinct ports")
    if any(not 1 <= p <= 65535 or p in RESERVED_PORTS for p in ports):
        raise DesiredStateError("ports: out of range or reserved")
    if not valid_domain(tls_domain):
        raise DesiredStateError("tls_domain must be a lower-case DNS name")
    _check_ad_tag(ad_tag)
    if not (classic or secure or tls):
        raise DesiredStateError("at least one Telemt mode must be on")
    values = {
        "ports": ports,
        "tls_domain": tls_domain,
        "ad_tag": ad_tag,
        "mode_classic": classic,
        "mode_secure": secure,
        "mode_tls": tls,
        "updated_at": datetime.now(UTC),
    }
    await session.execute(
        insert(TelemtNodeConfig)
        .values(node_id=node_id, **values)
        .on_conflict_do_update(index_elements=["node_id"], set_=values)
    )


async def create_telemt_user(
    session: AsyncSession,
    seal_key: bytes,
    node_id: uuid.UUID,
    name: str,
    *,
    ad_tag: str | None = None,
    expires_at: datetime | None = None,
    data_quota_bytes: int | None = None,
    max_tcp_conns: int | None = None,
    registration_id: uuid.UUID | None = None,
) -> TelemtUser:
    """Add a Telemt user with a fresh random secret (16 bytes = 32 hex characters, generated here,
    stored sealed). The caller commits and reads the secret back with :func:`reveal_secret` only
    where it is really needed (proxy link, @MTProxybot registration)."""
    if not valid_user_name(name):
        raise DesiredStateError("user name must match [a-z][a-z0-9_]{0,31} and not be reserved")
    _check_ad_tag(ad_tag)
    if expires_at is not None and expires_at.tzinfo is None:
        raise DesiredStateError("expires_at must be timezone-aware")
    if data_quota_bytes is not None and data_quota_bytes <= 0:
        raise DesiredStateError("data_quota_bytes must be positive")
    if max_tcp_conns is not None and max_tcp_conns <= 0:
        raise DesiredStateError("max_tcp_conns must be positive")
    count = (
        await session.execute(
            select(func.count()).select_from(TelemtUser).where(TelemtUser.node_id == node_id)
        )
    ).scalar_one()
    if count >= MAX_USERS_PER_NODE:
        raise DesiredStateError("too many Telemt users on this node")
    user = TelemtUser(
        node_id=node_id,
        name=name,
        secret_sealed=seal_secret(seal_key, node_id, name, secrets.token_bytes(16)),
        ad_tag=ad_tag,
        enabled=True,
        expires_at=expires_at,
        data_quota_bytes=data_quota_bytes,
        max_tcp_conns=max_tcp_conns,
        registration_id=registration_id,
    )
    session.add(user)
    await session.flush()
    return user


async def set_user_sponsor_tag(
    session: AsyncSession, node_id: uuid.UUID, name: str, ad_tag: str | None
) -> None:
    """Switch one secret's sponsor tag (hot-reloaded by Telemt, no restart; docs/11 E2)."""
    _check_ad_tag(ad_tag)
    user = await _get_user(session, node_id, name)
    user.ad_tag = ad_tag
    user.updated_at = datetime.now(UTC)


async def set_user_enabled(session: AsyncSession, node_id: uuid.UUID, name: str, enabled: bool) -> None:
    user = await _get_user(session, node_id, name)
    user.enabled = enabled
    user.updated_at = datetime.now(UTC)


async def reveal_secret(session: AsyncSession, seal_key: bytes, node_id: uuid.UUID, name: str) -> str:
    """The user's secret as 32 hex characters. Never log or store the result."""
    user = await _get_user(session, node_id, name)
    return open_secret(seal_key, node_id, name, user.secret_sealed).hex()


async def _get_user(session: AsyncSession, node_id: uuid.UUID, name: str) -> TelemtUser:
    user = (
        await session.execute(
            select(TelemtUser).where(TelemtUser.node_id == node_id, TelemtUser.name == name)
        )
    ).scalar_one_or_none()
    if user is None:
        raise DesiredStateError("no such Telemt user on this node")
    return user

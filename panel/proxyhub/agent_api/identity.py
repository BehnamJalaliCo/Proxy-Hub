"""Node agent identity: registration with a one-time token, certificate renewal, and resolving a
client-certificate fingerprint (forwarded by Caddy after mTLS) to a node.

Every decision is written to ``actions_log`` (actor ``agent_api``) and ``node_events``. Neither the
token nor any key material is ever logged: only node ids, certificate serials and short codes.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.agent_api.runtime import AgentRuntime
from proxyhub.agent_api.tokens import CLOSED_STATES, consume_registration_token
from proxyhub.db.models import ActionLog, Node, NodeEvent
from proxyhub.db.models_agent import AgentCertificate, AgentNode
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.pki.ca import spki_sha256, to_pem
from proxyhub.pki.issue import CsrError, IssuedCertificate, parse_csr, renewal_time, sign_node_csr
from proxyhub.pki.store import ensure_ca_certificate

ACTOR = "agent_api"
# A renewal may arrive this much before the two-thirds point (agent clock a little ahead).
RENEW_EARLY_TOLERANCE = timedelta(hours=1)
# last_seen_at moves at most this often: nodes poll every few seconds, and a row write per request
# (about 70 a second at 1000 nodes) buys nothing a heartbeat needs.
LAST_SEEN_RESOLUTION = timedelta(seconds=30)
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


class AgentRefused(Exception):
    """A registration or renewal was refused; ``code`` is safe to return and to log."""

    def __init__(self, code: str, status: int = 400, node_id: uuid.UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.node_id = node_id


@dataclass(frozen=True)
class AgentIdentity:
    node_id: uuid.UUID
    certificate_id: int
    not_before: datetime
    not_after: datetime


@dataclass(frozen=True)
class IssuedBundle:
    cert_pem: str
    ca_pem: str


def normalize_fingerprint(raw: str) -> str | None:
    """Accept ``ab12...`` or ``AB:12:...``; return 64 lower-case hex characters or ``None``."""
    value = raw.strip().replace(":", "").lower()
    return value if _FINGERPRINT.fullmatch(value) else None


async def audit(
    session: AsyncSession, action: str, target: str | None, allowed: bool, reasons: dict[str, Any]
) -> None:
    session.add(
        ActionLog(actor=ACTOR, action=action, target=target, dry_run=False, allowed=allowed, reasons=reasons)
    )


async def _issue(
    session: AsyncSession,
    runtime: AgentRuntime,
    node_id: uuid.UUID,
    csr_pem: str,
    now: datetime,
    via: str,
) -> tuple[IssuedCertificate, str]:
    csr = parse_csr(csr_pem, node_id)
    ca_cert = await ensure_ca_certificate(session, runtime.ca_key)
    issued = sign_node_csr(csr, node_id=node_id, ca_key=runtime.ca_key, ca_cert=ca_cert, now=now)
    session.add(
        AgentCertificate(
            node_id=node_id,
            fingerprint_sha256=issued.fingerprint_sha256,
            serial_hex=issued.serial_hex,
            ca_spki_sha256=spki_sha256(runtime.ca_key.public_key()),
            cert_pem=issued.pem,
            not_before=issued.not_before,
            not_after=issued.not_after,
            issued_via=via,
        )
    )
    return issued, to_pem(ca_cert)


async def register_node(
    session: AsyncSession,
    runtime: AgentRuntime,
    *,
    token: str,
    csr_pem: str,
    agent_version: str,
    telemt_version: str | None,
    now: datetime,
) -> IssuedBundle:
    """Use a one-time token to issue the node's first (or a replacement) client certificate.

    Raises :class:`AgentRefused` and leaves the transaction for the caller to roll back, which also
    leaves the token unused when only the CSR was wrong. The caller commits on success.
    """
    node_id = await consume_registration_token(session, token, now=now)
    if node_id is None:
        raise AgentRefused("invalid_token", status=401)
    state = (await session.execute(select(Node.state).where(Node.id == node_id))).scalar_one()
    if state in CLOSED_STATES:
        raise AgentRefused("node_closed", status=403, node_id=node_id)
    try:
        issued, ca_pem = await _issue(session, runtime, node_id, csr_pem, now, "register")
    except CsrError as exc:
        raise AgentRefused(exc.code, node_id=node_id) from None
    # A fresh registration replaces whatever identity the node had before (rebuilt server).
    await session.execute(
        update(AgentCertificate)
        .where(
            AgentCertificate.node_id == node_id,
            AgentCertificate.revoked_at.is_(None),
            AgentCertificate.fingerprint_sha256 != issued.fingerprint_sha256,
        )
        .values(revoked_at=now, revoked_reason="re_registered")
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        insert(AgentNode)
        .values(
            node_id=node_id,
            agent_version=agent_version,
            telemt_version=telemt_version,
            registered_at=now,
            last_seen_at=now,
        )
        .on_conflict_do_update(
            index_elements=["node_id"],
            set_={
                "agent_version": agent_version,
                "telemt_version": telemt_version,
                "registered_at": now,
                "last_seen_at": now,
                "last_served_etag": None,
                "last_applied_etag": None,
            },
        )
    )
    detail = {
        "agent_version": agent_version,
        "telemt_version": telemt_version,
        "serial": issued.serial_hex,
        "not_after": issued.not_after.isoformat(),
    }
    session.add(NodeEvent(node_id=node_id, kind="agent_registered", detail=detail))
    await audit(session, "agent_register", str(node_id), True, detail)
    return IssuedBundle(cert_pem=issued.pem, ca_pem=ca_pem)


async def renew_certificate(
    session: AsyncSession, runtime: AgentRuntime, identity: AgentIdentity, csr_pem: str, now: datetime
) -> IssuedBundle:
    """Issue a successor certificate to an authenticated agent from two thirds of its lifetime on.

    The presenting certificate stays valid until the new one is first used (see
    :func:`authenticate`), so a lost response never locks a node out. The caller commits.
    """
    due = renewal_time(identity.not_before, identity.not_after) - RENEW_EARLY_TOLERANCE
    if now < due:
        raise AgentRefused("renewal_too_early", status=409, node_id=identity.node_id)
    try:
        issued, ca_pem = await _issue(session, runtime, identity.node_id, csr_pem, now, "renew")
    except CsrError as exc:
        raise AgentRefused(exc.code, node_id=identity.node_id) from None
    detail = {"serial": issued.serial_hex, "not_after": issued.not_after.isoformat()}
    session.add(NodeEvent(node_id=identity.node_id, kind="agent_cert_renewed", detail=detail))
    await audit(session, "agent_renew", str(identity.node_id), True, detail)
    return IssuedBundle(cert_pem=issued.pem, ca_pem=ca_pem)


async def authenticate(session: AsyncSession, fingerprint: str, now: datetime) -> AgentIdentity | None:
    """Resolve a verified client-certificate fingerprint to a node identity.

    Only a certificate we issued, unrevoked, inside its validity window, for a node that is not
    deleted, is accepted. Using a renewed certificate retires every older one of the same node.
    The caller commits.
    """
    row = (
        await session.execute(
            select(
                AgentCertificate.id,
                AgentCertificate.node_id,
                AgentCertificate.not_before,
                AgentCertificate.not_after,
            )
            .join(Node, Node.id == AgentCertificate.node_id)
            .where(
                AgentCertificate.fingerprint_sha256 == fingerprint,
                AgentCertificate.revoked_at.is_(None),
                AgentCertificate.not_before <= now,
                AgentCertificate.not_after > now,
                Node.state != NodeState.DELETED.value,
            )
        )
    ).first()
    if row is None:
        return None
    cert_id, node_id, not_before, not_after = row
    await session.execute(
        update(AgentCertificate)
        .where(
            AgentCertificate.node_id == node_id,
            AgentCertificate.id < cert_id,
            AgentCertificate.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_reason="superseded")
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentNode)
        .where(
            AgentNode.node_id == node_id,
            or_(AgentNode.last_seen_at.is_(None), AgentNode.last_seen_at <= now - LAST_SEEN_RESOLUTION),
        )
        .values(last_seen_at=now)
    )
    return AgentIdentity(node_id=node_id, certificate_id=cert_id, not_before=not_before, not_after=not_after)

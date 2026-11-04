"""Sponsor contract (cross-team contract item 4): what team sponsor registers in @MTProxybot, and
where the tag it gets back goes.

* :func:`proxies_to_register`: live proxies of nodes in service that have no registration yet. A
  proxy counts as registered when its Telemt user already has a sponsor tag or a linked
  ``mtproxy_registrations`` row, or when such a row (state ``pending`` or ``registered``) names it in
  ``secret_name`` (the Telemt user name, unique per node, or the proxy key).
* :func:`apply_ad_tag`: the tag goes to that proxy's own Telemt user (``access.user_ad_tags``, hot
  reloaded without a restart, docs/11 E2), so one node can carry secrets with different sponsors.

Both run inside the caller's transaction and never commit it. The secret is handed out only here and
in the public link (rule 3); ``actions_log`` gets neither the secret nor the tag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.desired import DesiredStateError, set_user_sponsor_tag
from proxyhub.agent_api.validation import HEX32
from proxyhub.db.models import MtproxyRegistration, Node
from proxyhub.db.models_agent import TelemtUser
from proxyhub.db.models_mtproto import MtprotoProxy
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.mtproto.keys import load_proxy_seal_key
from proxyhub.mtproto.service import list_proxies
from proxyhub.mtproto.urls import parse_key

LIVE_REGISTRATION_STATES = ("pending", "registered")


@dataclass(frozen=True)
class RegistrableProxy:
    key: str
    host: str
    port: int
    secret_hex: str = field(repr=False)  # 32 lower-case hex, no ee/dd prefix


class UnknownProxy(LookupError):
    """No public MTProto proxy has this key."""


async def _registered_names(db: AsyncConnection) -> set[str]:
    users = (
        await db.execute(
            select(TelemtUser.name).where(
                or_(TelemtUser.ad_tag.is_not(None), TelemtUser.registration_id.is_not(None))
            )
        )
    ).scalars()
    rows = (
        await db.execute(
            select(MtproxyRegistration.secret_name).where(
                MtproxyRegistration.state.in_(LIVE_REGISTRATION_STATES)
            )
        )
    ).scalars()
    return {*users, *rows}


async def proxies_to_register(db: AsyncConnection) -> list[RegistrableProxy]:
    """Live proxies on ready nodes without a registration, slot order."""
    views = await list_proxies(db, load_proxy_seal_key(), datetime.now(UTC))
    taken = await _registered_names(db)
    return [
        RegistrableProxy(key=v.key, host=v.host, port=v.port, secret_hex=v.secret_hex)
        for v in views
        if v.status == "active"
        and v.node_state == NodeState.READY.value
        and v.host is not None
        and v.user not in taken
        and v.key not in taken
    ]


async def apply_ad_tag(db: AsyncConnection, key: str, ad_tag: str | None) -> None:
    """Put ``ad_tag`` (32 lower-case hex from @MTProxybot, or None to clear it) on the proxy's own
    Telemt user in the node's desired state. Raises :class:`UnknownProxy` or ``ValueError``."""
    if ad_tag is not None and not HEX32.fullmatch(ad_tag):
        raise DesiredStateError("ad_tag must be 32 lower-case hex characters")
    parsed = parse_key(key)
    if parsed is None:
        raise UnknownProxy("not an MTProto proxy key")
    node_id, user = parsed
    known = (
        await db.execute(
            select(Node.name)
            .join(MtprotoProxy, MtprotoProxy.node_id == Node.id)
            .where(MtprotoProxy.node_id == node_id, MtprotoProxy.user_name == user)
        )
    ).scalar_one_or_none()
    if known is None:
        raise UnknownProxy("no such MTProto proxy")
    # create_savepoint: the session works in a SAVEPOINT of the caller's transaction, and commit()
    # only releases it. Without the commit, a caller already inside a SAVEPOINT (begin_nested) would
    # lose the tag when the session closes while the registration link below still got written.
    session = AsyncSession(bind=db, join_transaction_mode="create_savepoint")
    try:
        await set_user_sponsor_tag(session, node_id, user, ad_tag)
        await session.commit()
    finally:
        await session.close()
    # Node counters flow into mtproxy_stats once the user points at its registration.
    registration = None
    if ad_tag is not None:
        registration = (
            (
                await db.execute(
                    select(MtproxyRegistration.id).where(
                        MtproxyRegistration.secret_name.in_([user, key]),
                        MtproxyRegistration.state == "registered",
                        MtproxyRegistration.ad_tag == ad_tag,
                    )
                )
            )
            .scalars()
            .first()
        )
    await db.execute(
        update(TelemtUser)
        .where(TelemtUser.node_id == node_id, TelemtUser.name == user)
        .values(registration_id=registration)
    )
    await log_action(
        db,
        actor="mtproto:sponsor_api",
        action="mtproto_ad_tag",
        target=known,
        dry_run=False,
        allowed=True,
        reasons=["tag_set" if ad_tag is not None else "tag_cleared"],
        context={"node_id": str(node_id), "user": user, "registration_linked": registration is not None},
        result="sponsor tag written to the proxy's desired Telemt state (tag not logged)",
    )

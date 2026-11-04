"""The sponsor module's rows: settings, registrations with their flow bookkeeping, campaigns.

* ``settings`` key :data:`SETTINGS_KEY`: ``{"default_channel", "lead_minutes"}`` (owner-editable).
* ``sponsor_flows`` (one row per proxy the module works on) with the ``mtproxy_registrations`` row
  it led to, read together as :class:`Reg`. A registration is an entry in @MTProxybot: its row is
  written only when the bot gave the tag, so until then the proxy stays "without a registration"
  for team mtproto's ``proxies_to_register`` (the cross-team contract), which keeps listing it with
  its secret. A registration is identified by ``host:port`` and the Telemt user name (the table's
  unique key); a provider that lists the same user at the same address again (team mtproto's user
  names are per node, so it does not) gets the existing entry and its tag back.
* ``sponsor_campaigns`` as :class:`~proxyhub.sponsor.campaigns.Window` for the pure logic.

Nothing here talks to Telegram or logs; callers own the transactions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import and_, case, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.db.models import MtproxyRegistration, Node, Setting, SponsorCampaign
from proxyhub.db.models_sponsor import SponsorFlow
from proxyhub.sponsor.campaigns import DEFAULT_LEAD, Window
from proxyhub.sponsor.channels import DEFAULT_CHANNEL, ChannelError, parse_public_channel

SETTINGS_KEY: Final = "sponsor.settings"
MIN_LEAD_MIN: Final = 0
MAX_LEAD_MIN: Final = 360

# Registration states (the CHECK constraint of mtproxy_registrations).
PENDING: Final = "pending"
REGISTERED: Final = "registered"
FAILED: Final = "failed"
RETIRED: Final = "retired"


@dataclass(frozen=True)
class SponsorSettings:
    default_channel: str = DEFAULT_CHANNEL
    lead_minutes: int = int(DEFAULT_LEAD.total_seconds() // 60)

    @property
    def lead(self) -> timedelta:
        return timedelta(minutes=self.lead_minutes)

    def to_json(self) -> dict[str, Any]:
        return {"default_channel": self.default_channel, "lead_minutes": self.lead_minutes}

    @classmethod
    def from_json(cls, value: Any) -> SponsorSettings:
        """A stored row; anything unreadable falls back to the default, field by field."""
        if not isinstance(value, dict):
            return cls()
        channel = cls.default_channel
        try:
            channel = parse_public_channel(str(value.get("default_channel", "")))
        except ChannelError:
            pass
        lead = value.get("lead_minutes")
        if not isinstance(lead, int) or isinstance(lead, bool) or not MIN_LEAD_MIN <= lead <= MAX_LEAD_MIN:
            lead = cls.lead_minutes
        return cls(default_channel=channel, lead_minutes=lead)


async def load_settings(db: AsyncConnection) -> SponsorSettings:
    row = (await db.execute(select(Setting.value).where(Setting.key == SETTINGS_KEY))).first()
    return SponsorSettings() if row is None else SponsorSettings.from_json(row[0])


async def save_settings(db: AsyncConnection, value: SponsorSettings) -> None:
    stmt = insert(Setting).values(key=SETTINGS_KEY, value=value.to_json())
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_at": text("now()")}
        )
    )


# --- registrations ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reg:
    """One proxy the module works on: its ``sponsor_flows`` row with the ``mtproxy_registrations``
    row it led to, if any. ``id`` is the flow row's (stable from the first listing on, also across
    the registration); ``registration_id`` is None while @MTProxybot has not given a tag (a
    *candidate*, whose state reads ``pending``, or ``failed`` while it is stuck)."""

    id: uuid.UUID
    registration_id: uuid.UUID | None
    host: str
    port: int
    secret_name: str
    ad_tag: str | None
    sponsor_channel: str | None
    state: str
    state_reason: str | None
    registered_at: datetime | None
    created_at: datetime
    proxy_key: str | None
    attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    last_step: str | None
    stuck_reason: str | None
    stuck_hint: str | None
    stuck_at: datetime | None
    last_bot_text: str | None
    alerted_at: datetime | None
    promotion_set_at: datetime | None
    tag_applied_at: datetime | None
    tag_failed_at: datetime | None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def candidate(self) -> bool:
        return self.registration_id is None

    @property
    def stuck(self) -> bool:
        """The last conversation stopped on a reply the panel does not understand (owner alerted)."""
        return self.stuck_reason is not None and self.state != RETIRED

    def due(self, now: datetime) -> bool:
        return self.next_attempt_at is None or self.next_attempt_at <= now


_R = MtproxyRegistration.__table__.c
_F = SponsorFlow.__table__.c


def _reg_query() -> Any:
    state = case(
        (_R.id.is_(None), case((_F.stuck_reason.is_not(None), FAILED), else_=PENDING)),
        else_=_R.state,
    )
    return select(
        _F.id,
        _F.registration_id,
        func.coalesce(_R.host, _F.host).label("host"),
        func.coalesce(_R.port, _F.port).label("port"),
        func.coalesce(_R.secret_name, _F.user_name).label("secret_name"),
        _R.ad_tag,
        _R.sponsor_channel,
        state.label("state"),
        _R.state_reason,
        _R.registered_at,
        func.coalesce(_R.created_at, _F.created_at).label("created_at"),
        _F.proxy_key,
        _F.attempts,
        _F.next_attempt_at,
        _F.last_error,
        _F.last_step,
        _F.stuck_reason,
        _F.stuck_hint,
        _F.stuck_at,
        _F.last_bot_text,
        _F.alerted_at,
        _F.promotion_set_at,
        _F.tag_applied_at,
        _F.tag_failed_at,
    ).select_from(SponsorFlow.__table__.outerjoin(MtproxyRegistration.__table__, _R.id == _F.registration_id))


def _reg(row: Any) -> Reg:
    m = row._mapping
    return Reg(
        id=m["id"],
        registration_id=m["registration_id"],
        host=str(m["host"]),
        port=int(m["port"]),
        secret_name=str(m["secret_name"]),
        ad_tag=m["ad_tag"],
        sponsor_channel=m["sponsor_channel"],
        state=str(m["state"]),
        state_reason=m["state_reason"],
        registered_at=m["registered_at"],
        created_at=m["created_at"],
        proxy_key=m["proxy_key"],
        attempts=int(m["attempts"] or 0),
        next_attempt_at=m["next_attempt_at"],
        last_error=m["last_error"],
        last_step=m["last_step"],
        stuck_reason=m["stuck_reason"],
        stuck_hint=m["stuck_hint"],
        stuck_at=m["stuck_at"],
        last_bot_text=m["last_bot_text"],
        alerted_at=m["alerted_at"],
        promotion_set_at=m["promotion_set_at"],
        tag_applied_at=m["tag_applied_at"],
        tag_failed_at=m["tag_failed_at"],
    )


async def load_regs(db: AsyncConnection, *, for_update: bool = False) -> list[Reg]:
    stmt = _reg_query().order_by(func.coalesce(_R.created_at, _F.created_at), _F.id)
    if for_update:
        stmt = stmt.with_for_update(of=SponsorFlow.__table__)
    return [_reg(r) for r in (await db.execute(stmt)).all()]


async def load_reg(db: AsyncConnection, flow_id: uuid.UUID, *, for_update: bool = False) -> Reg | None:
    stmt = _reg_query().where(_F.id == flow_id)
    if for_update:
        stmt = stmt.with_for_update(of=SponsorFlow.__table__)
    row = (await db.execute(stmt)).first()
    return None if row is None else _reg(row)


async def adopt_registrations(db: AsyncConnection, now: datetime) -> None:
    """A flow row for every registration that has none (one written by other code): shown on the
    dashboard and matched by address, never worked on without a live proxy."""
    await db.execute(
        text(
            "INSERT INTO sponsor_flows (id, registration_id, host, port, user_name, created_at, updated_at) "
            "SELECT r.id, r.id, r.host, r.port, left(r.secret_name, 64), r.created_at, :now "
            "FROM mtproxy_registrations r WHERE r.port BETWEEN 1 AND 65535 AND NOT EXISTS "
            "(SELECT 1 FROM sponsor_flows f WHERE f.registration_id = r.id) ON CONFLICT DO NOTHING"
        ),
        {"now": now},
    )


async def insert_candidate(
    db: AsyncConnection, *, host: str, port: int, user_name: str, proxy_key: str | None, now: datetime
) -> uuid.UUID:
    """A proxy without a registration: no ``mtproxy_registrations`` row until it has a tag."""
    flow_id = uuid.uuid4()
    await db.execute(
        insert(SponsorFlow).values(
            id=flow_id,
            host=host,
            port=port,
            user_name=user_name[:64],
            proxy_key=proxy_key,
            created_at=now,
            updated_at=now,
        )
    )
    return flow_id


async def create_registration(
    db: AsyncConnection,
    flow_id: uuid.UUID,
    *,
    host: str,
    port: int,
    secret_name: str,
    ad_tag: str,
    now: datetime,
    state: str = REGISTERED,
    state_reason: str | None = None,
) -> uuid.UUID:
    """The entry @MTProxybot just made (its tag) as a ``mtproxy_registrations`` row, linked to the
    flow. A row with the same ``host:port`` and user already there (the table's unique key) takes
    the new tag, and its old flow row gives way to this one."""
    values: dict[str, Any] = {
        "ad_tag": ad_tag,
        "state": state,
        "state_reason": state_reason,
        "registered_at": now,
        "sponsor_channel": None,
        "updated_at": now,
    }
    existing = (
        await db.execute(
            select(_R.id)
            .where(_R.host == host, _R.port == port, _R.secret_name == secret_name[:64])
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is None:
        reg_id = flow_id
        await db.execute(
            insert(MtproxyRegistration).values(
                id=reg_id, host=host, port=port, secret_name=secret_name[:64], created_at=now, **values
            )
        )
    else:
        reg_id = existing
        await db.execute(update(MtproxyRegistration).where(_R.id == reg_id).values(**values))
        await db.execute(delete(SponsorFlow).where(_F.registration_id == reg_id, _F.id != flow_id))
    await db.execute(
        update(SponsorFlow).where(_F.id == flow_id).values(registration_id=reg_id, updated_at=now)
    )
    return reg_id


async def update_reg(
    db: AsyncConnection, registration_id: uuid.UUID | None, now: datetime, **values: Any
) -> None:
    """Change the ``mtproxy_registrations`` row (nothing for a candidate, which has none)."""
    if registration_id is None:
        return
    await db.execute(
        update(MtproxyRegistration).where(_R.id == registration_id).values(updated_at=now, **values)
    )


async def update_flow(db: AsyncConnection, flow_id: uuid.UUID, now: datetime, **values: Any) -> None:
    await db.execute(update(SponsorFlow).where(_F.id == flow_id).values(updated_at=now, **values))


async def delete_flow(db: AsyncConnection, flow_id: uuid.UUID) -> None:
    """Only for a candidate (nothing was registered in @MTProxybot for it)."""
    await db.execute(delete(SponsorFlow).where(_F.id == flow_id, _F.registration_id.is_(None)))


CLEAR_STUCK: Final[dict[str, Any]] = {
    "stuck_reason": None,
    "stuck_hint": None,
    "stuck_at": None,
    "last_bot_text": None,
    "alerted_at": None,
}


async def node_states(db: AsyncConnection, node_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not node_ids:
        return {}
    rows = await db.execute(select(Node.id, Node.state).where(Node.id.in_(node_ids)))
    return {r[0]: str(r[1]) for r in rows}


# --- campaigns ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Campaign:
    id: uuid.UUID
    customer: str
    channel: str
    starts_at: datetime
    ends_at: datetime
    price: Decimal | None
    currency: str | None
    paid: bool
    state: str
    created_at: datetime
    updated_at: datetime

    @property
    def window(self) -> Window:
        return Window(
            id=self.id, channel=self.channel, starts_at=self.starts_at, ends_at=self.ends_at, state=self.state
        )


_C = SponsorCampaign.__table__.c


def _campaign(row: Any) -> Campaign:
    m = row._mapping
    return Campaign(
        id=m["id"],
        customer=str(m["customer"]),
        channel=str(m["channel"]),
        starts_at=m["starts_at"],
        ends_at=m["ends_at"],
        price=m["price"],
        currency=m["currency"],
        paid=bool(m["paid"]),
        state=str(m["state"]),
        created_at=m["created_at"],
        updated_at=m["updated_at"],
    )


async def load_campaigns(db: AsyncConnection, *, for_update: bool = False) -> list[Campaign]:
    stmt = select(SponsorCampaign.__table__).order_by(_C.starts_at.desc(), _C.id)
    if for_update:
        stmt = stmt.with_for_update()
    return [_campaign(r) for r in (await db.execute(stmt)).all()]


async def load_campaign(
    db: AsyncConnection, campaign_id: uuid.UUID, *, for_update: bool = False
) -> Campaign | None:
    stmt = select(SponsorCampaign.__table__).where(_C.id == campaign_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    return None if row is None else _campaign(row)


async def lock_campaigns(db: AsyncConnection) -> None:
    """Serialise campaign writes (the overlap check reads every open campaign)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('proxyhub:sponsor_campaigns'))"))


async def set_campaign_state(db: AsyncConnection, campaign_id: uuid.UUID, state: str, now: datetime) -> None:
    await db.execute(
        update(SponsorCampaign).where(and_(_C.id == campaign_id)).values(state=state, updated_at=now)
    )

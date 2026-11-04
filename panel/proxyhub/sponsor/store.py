"""The sponsor module's rows: settings, registrations with their flow bookkeeping, campaigns.

* ``settings`` key :data:`SETTINGS_KEY`: ``{"default_channel", "lead_minutes"}`` (owner-editable).
* ``mtproxy_registrations`` + ``sponsor_flows`` (one flow row per registration), read together as
  :class:`Reg`. A registration is an entry in @MTProxybot, identified by ``host:port`` and the
  Telemt user name (the table's unique key): when a node is replaced and the new one answers at the
  same slot name and port with the same user name, the entry (and its tag) is reused instead of
  registering again.
* ``sponsor_campaigns`` as :class:`~proxyhub.sponsor.campaigns.Window` for the pure logic.

Nothing here talks to Telegram or logs; callers own the transactions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import and_, select, text, update
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
    id: uuid.UUID
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

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def due(self, now: datetime) -> bool:
        return self.next_attempt_at is None or self.next_attempt_at <= now


_R = MtproxyRegistration.__table__.c
_F = SponsorFlow.__table__.c


def _reg_query() -> Any:
    return select(
        _R.id,
        _R.host,
        _R.port,
        _R.secret_name,
        _R.ad_tag,
        _R.sponsor_channel,
        _R.state,
        _R.state_reason,
        _R.registered_at,
        _R.created_at,
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
    ).select_from(MtproxyRegistration.__table__.outerjoin(SponsorFlow.__table__, _F.registration_id == _R.id))


def _reg(row: Any) -> Reg:
    m = row._mapping
    return Reg(
        id=m["id"],
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
    )


async def load_regs(db: AsyncConnection, *, for_update: bool = False) -> list[Reg]:
    stmt = _reg_query().order_by(_R.created_at, _R.id)
    if for_update:
        stmt = stmt.with_for_update(of=MtproxyRegistration.__table__)
    return [_reg(r) for r in (await db.execute(stmt)).all()]


async def load_reg(db: AsyncConnection, reg_id: uuid.UUID, *, for_update: bool = False) -> Reg | None:
    stmt = _reg_query().where(_R.id == reg_id)
    if for_update:
        stmt = stmt.with_for_update(of=MtproxyRegistration.__table__)
    row = (await db.execute(stmt)).first()
    return None if row is None else _reg(row)


async def insert_reg(
    db: AsyncConnection, *, host: str, port: int, secret_name: str, proxy_key: str, now: datetime
) -> uuid.UUID:
    reg_id = uuid.uuid4()
    await db.execute(
        insert(MtproxyRegistration).values(
            id=reg_id,
            host=host,
            port=port,
            secret_name=secret_name[:64],
            state=PENDING,
            created_at=now,
            updated_at=now,
        )
    )
    await db.execute(insert(SponsorFlow).values(registration_id=reg_id, proxy_key=proxy_key, updated_at=now))
    return reg_id


async def update_reg(db: AsyncConnection, reg_id: uuid.UUID, now: datetime, **values: Any) -> None:
    await db.execute(update(MtproxyRegistration).where(_R.id == reg_id).values(updated_at=now, **values))


async def update_flow(db: AsyncConnection, reg_id: uuid.UUID, now: datetime, **values: Any) -> None:
    """Upsert the flow row of a registration (a registration made by other code may have none)."""
    stmt = insert(SponsorFlow).values(registration_id=reg_id, updated_at=now, **values)
    await db.execute(
        stmt.on_conflict_do_update(index_elements=[_F.registration_id], set_={"updated_at": now, **values})
    )


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

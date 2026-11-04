"""MTProto team table: the one long-lived Telemt user (proxy secret) the policy keeps on each node.

The secret itself lives only in ``telemt_users.secret_sealed`` (AES-256-GCM, rule 3); this row only
says which Telemt user (``telemt_users`` row ``node_id`` + ``user_name``) is the node's public proxy
and when the node's agent first ran it. There is deliberately no foreign key to ``telemt_users``
(the agent tables are truncated as a group); the policy recreates a missing user. Nothing here is
about end users (rule 7).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base
from proxyhub.db.models_agent import TELEMT_USER_NAME


class MtprotoProxy(Base):
    __tablename__ = "mtproto_proxies"
    __table_args__ = (
        CheckConstraint(f"user_name ~ '{TELEMT_USER_NAME}'", name="user_name"),
        CheckConstraint("live_at IS NULL OR live_at >= created_at", name="live_after_created"),
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    user_name: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # First time the node's agent reported that it applied a desired state holding this user with
    # Telemt up: from then on the link works (until the node leaves service).
    live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the policy disabled the user because its node is retiring or deleted.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

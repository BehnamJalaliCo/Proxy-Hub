"""MTProto team table: the Telemt users (proxy secrets) the policy keeps on each node for the posts.

``ordinal`` 0 is the node's PRIMARY user, its one public proxy that is registered with @MTProxybot.
``ordinal`` 1 and up are EXTRA users (decision D49): more links into the same proxy server (the
node's port and host), each with its own secret and the primary's sponsor tag, so a post can carry
six links while fewer nodes run. An extra is never registered and never removed while its node
lives (a posted link must keep working).

The secret itself lives only in ``telemt_users.secret_sealed`` (AES-256-GCM, rule 3); a row here
only says which Telemt user (``telemt_users`` row ``node_id`` + ``user_name``) is one of the node's
public proxies and when the node's agent first ran it. There is deliberately no foreign key to
``telemt_users`` (the agent tables are truncated as a group); the policy recreates a missing user.
Nothing here is about end users (rule 7).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base
from proxyhub.db.models_agent import TELEMT_USER_NAME

# A node keeps at most this many public links (its primary + extras, decision D49).
MAX_LINKS_PER_NODE = 6


class MtprotoProxy(Base):
    __tablename__ = "mtproto_proxies"
    __table_args__ = (
        CheckConstraint(f"user_name ~ '{TELEMT_USER_NAME}'", name="user_name"),
        CheckConstraint("live_at IS NULL OR live_at >= created_at", name="live_after_created"),
        CheckConstraint(f"ordinal >= 0 AND ordinal < {MAX_LINKS_PER_NODE}", name="ordinal"),
        UniqueConstraint("node_id", "ordinal", name="uq_mtproto_proxies_node_ordinal"),
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    user_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 0: the node's primary (registered) user; 1 and up: its extras (decision D49).
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # First time the node's agent reported that it applied a desired state holding this user with
    # Telemt up: from then on the link works (until the node leaves service).
    live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the policy disabled the user because its node is retiring or deleted.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

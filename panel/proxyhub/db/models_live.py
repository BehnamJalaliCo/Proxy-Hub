"""Live node executor tables (team live): what the executor did to one node and when.

The node row itself (``nodes``) holds the lifecycle state; this table holds the executor's own
bookkeeping: who asked for the node, when a Hetzner create was attempted, when the node entered its
current state (for timeouts), and why it is being retired. Nothing here is secret: the one-time
registration token exists only as SHA-256 in ``agent_registration_tokens`` and in the server's
user data, never here. Nothing here is about end users (CLAUDE.md rule 7).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from proxyhub.db.models import Base

RETIRE_REASONS: tuple[str, ...] = ("owner", "unhealthy_test_node", "create_failed")
# Round 3, the live fleet (proxyhub/fleet/live.py, proxyhub/nodes/executor.py; migration 0013_fleetlive).
RETIRE_REASONS += ("burned", "unhealthy", "duplicate", "excess", "burned_ip")


class NodeOperation(Base):
    __tablename__ = "node_operations"
    __table_args__ = (
        CheckConstraint("create_attempts >= 0 AND delete_attempts >= 0", name="attempts"),
        CheckConstraint(
            f"retire_reason IS NULL OR retire_reason IN ({', '.join(repr(r) for r in RETIRE_REASONS)})",
            name="retire_reason",
        ),
        CheckConstraint("(retire_requested_at IS NULL) = (retire_reason IS NULL)", name="retire_pair"),
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    # "admin:<username>" or "cli"; never anything secret.
    requested_by: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the node entered nodes.state (timeouts are measured from here).
    state_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    create_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    server_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retire_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retire_reason: Mapped[str | None] = mapped_column(String(40))
    delete_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Short machine code of the last failure (a Hetzner error code or a gate reason), never a text
    # that could hold a token or user data.
    last_error: Mapped[str | None] = mapped_column(String(60))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

"""content supervisor and QC (decision D52; docs/10 section 3.12, M3.1 phase B)

* ``content_candidates``: the supervisor's review of a candidate (``qc_decision``, ``qc_reasons``,
  ``qc_flags``, ``qc_checks``, ``qc_category``, ``qc_error``, ``qc_backend``, ``qc_model``,
  ``qc_at``, ``qc_tries``) and who decided it (``decided_by``: ``owner`` or ``qc``). Candidates the
  owner already approved or rejected are marked ``owner``.
* ``content_sources``: why the supervisor turned a source off (``disabled_reason``, ``disabled_at``,
  ``disabled_detail``) and from when its quality is judged again (``quality_reset_at``).
* ``content_qc_calls``: one row per attempt to reach Claude (backend, outcome, tokens, time), for the
  daily caps and the dashboard. Nothing of the content.

Downgrade drops all of it; the review queue keeps working as in phase A (owner approval).

Revision ID: 0028_content_qc
Revises: 0027_content_engine
Create Date: 2026-11-07 18:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0028_content_qc"
down_revision = "0027_content_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_candidates", sa.Column("qc_decision", sa.String(length=8), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_checks", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_category", sa.String(length=24), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_error", sa.String(length=40), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_backend", sa.String(length=8), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_model", sa.String(length=64), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("content_candidates", sa.Column("qc_tries", sa.Integer(), server_default="0", nullable=False))
    op.add_column("content_candidates", sa.Column("decided_by", sa.String(length=8), nullable=True))
    op.create_check_constraint(
        op.f("ck_content_candidates_qc_decision"),
        "content_candidates",
        "qc_decision IS NULL OR qc_decision IN ('approve', 'reject')",
    )
    op.create_check_constraint(
        op.f("ck_content_candidates_qc_backend"),
        "content_candidates",
        "qc_backend IS NULL OR qc_backend IN ('bridge', 'api', 'rules')",
    )
    op.create_check_constraint(
        op.f("ck_content_candidates_decided_by"),
        "content_candidates",
        "decided_by IS NULL OR decided_by IN ('owner', 'qc')",
    )
    op.create_check_constraint(op.f("ck_content_candidates_qc_tries"), "content_candidates", "qc_tries >= 0")
    op.create_index(
        "ix_content_candidates_source_qc", "content_candidates", ["source_id", "qc_at"], unique=False
    )
    op.execute(
        "UPDATE content_candidates SET decided_by = 'owner' "
        "WHERE status IN ('approved', 'rejected', 'used') AND reviewed_at IS NOT NULL"
    )

    op.add_column("content_sources", sa.Column("disabled_reason", sa.String(length=40), nullable=True))
    op.add_column("content_sources", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "content_sources", sa.Column("disabled_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.add_column("content_sources", sa.Column("quality_reset_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "content_qc_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("backend", sa.String(length=8), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("reached", sa.Boolean(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Double(), nullable=True),
        sa.CheckConstraint("backend IN ('bridge', 'api')", name=op.f("ck_content_qc_calls_backend")),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_content_qc_calls_candidate_id_content_candidates"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_qc_calls")),
    )
    op.create_index("ix_content_qc_calls_backend_at", "content_qc_calls", ["backend", "at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_content_qc_calls_backend_at", table_name="content_qc_calls")
    op.drop_table("content_qc_calls")
    for col in ("quality_reset_at", "disabled_detail", "disabled_at", "disabled_reason"):
        op.drop_column("content_sources", col)
    op.drop_index("ix_content_candidates_source_qc", table_name="content_candidates")
    for name in ("qc_tries", "decided_by", "qc_backend", "qc_decision"):
        op.drop_constraint(op.f(f"ck_content_candidates_{name}"), "content_candidates", type_="check")
    for col in (
        "decided_by",
        "qc_tries",
        "qc_at",
        "qc_model",
        "qc_backend",
        "qc_error",
        "qc_category",
        "qc_checks",
        "qc_flags",
        "qc_reasons",
        "qc_decision",
    ):
        op.drop_column("content_candidates", col)

"""wstls: VLESS + WebSocket + TLS with the panel's own certificate (ACME DNS-01) for every node name

* ``reality_keys.ws_path``: the scope's WebSocket path ("/" + 16 random hex), kept with the key so a
  slot's links keep their path on a new server. Existing keys get a random one here.
* ``reality_configs.transport``: ``reality`` (every existing config) or ``ws_tls``.
* ``cf_records.purpose`` may be ``acme`` (the ``_acme-challenge`` TXT records of an order).
* ``acme_certificates``: per pool domain, the public facts of its wildcard certificate and the order
  bookkeeping; the chain, its key and the account key are in the vault only.

Revision ID: 0018_wstls
Revises: 0017_merge
Create Date: 2026-11-05 12:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_wstls"
down_revision = "0017_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reality_keys", sa.Column("ws_path", sa.String(length=17), nullable=True))
    # 64 random bits from PostgreSQL's strong random source (gen_random_uuid, built in since PG 13):
    # the first 16 hex digits of a version-4 UUID hold 60 random bits and the fixed version digit.
    op.execute(
        "UPDATE reality_keys SET ws_path = '/' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 16)"
    )
    op.alter_column("reality_keys", "ws_path", nullable=False)
    op.create_check_constraint(op.f("ck_reality_keys_ws_path"), "reality_keys", "ws_path ~ '^/[0-9a-f]{16}$'")

    op.add_column(
        "reality_configs",
        sa.Column("transport", sa.String(length=10), server_default="reality", nullable=False),
    )
    op.create_check_constraint(op.f("ck_reality_configs_transport"), "reality_configs", "transport IN ('reality', 'ws_tls')")

    op.drop_constraint(op.f("ck_cf_records_purpose"), "cf_records", type_="check")
    op.create_check_constraint(op.f("ck_cf_records_purpose"), "cf_records", "purpose IN ('slot', 'test', 'acme')")

    op.create_table(
        "acme_certificates",
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("zone_id", sa.String(length=32), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("serial_hex", sa.String(length=64), nullable=True),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=60), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_acme_certificates_attempts")),
        sa.CheckConstraint(
            "(not_before IS NULL) = (not_after IS NULL)", name=op.f("ck_acme_certificates_validity_pair")
        ),
        sa.CheckConstraint(
            "fingerprint_sha256 IS NULL OR fingerprint_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_acme_certificates_fp_hex"),
        ),
        sa.PrimaryKeyConstraint("domain", name=op.f("pk_acme_certificates")),
    )


def downgrade() -> None:
    op.drop_table("acme_certificates")
    op.execute("DELETE FROM cf_records WHERE purpose = 'acme'")
    op.drop_constraint(op.f("ck_cf_records_purpose"), "cf_records", type_="check")
    op.create_check_constraint(op.f("ck_cf_records_purpose"), "cf_records", "purpose IN ('slot', 'test')")
    op.drop_constraint(op.f("ck_reality_configs_transport"), "reality_configs", type_="check")
    op.drop_column("reality_configs", "transport")
    op.drop_constraint(op.f("ck_reality_keys_ws_path"), "reality_keys", type_="check")
    op.drop_column("reality_keys", "ws_path")

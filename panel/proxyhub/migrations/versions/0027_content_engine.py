"""content engine, phase A (decision D51; docs/10 section 3.12, M3.1)

* ``content_sources``: the owner's sources: a public Telegram channel (read from its public preview
  page, no Telegram account) or an RSS/Atom feed, each with its state (last read, last error code,
  failures in a row, next read).
* ``content_candidates``: one row per post of a source (``source_id`` + ``external_id`` unique),
  normalized: plain text, a checked https link, time, views and a media reference. The owner's
  review queue (decision D20) works on these: pending, approved, rejected, used, expired, or a
  duplicate of another source's candidate.
* ``content_media``: the photo, video or video thumbnail the panel downloaded for a candidate (the
  dashboard shows them through the panel's API; they are removed once the post went out).
* ``posts.content_candidate_id`` and ``posts.media_kind``: the candidate a post carries and whether
  the post is a photo or a video with a caption (a status edit then edits the caption).

Downgrade drops all of it: posts made from candidates keep their content (``posts.content_html``);
a photo or video post's later status edits would then go out as text edits and fail (so do not
downgrade while such posts are younger than the edit window).

Revision ID: 0027_content_engine
Revises: 0026_post_link_operator
Create Date: 2026-11-07 10:00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_content_engine"
down_revision = "0026_post_link_operator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "content_sources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ref", sa.String(length=512), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_fetch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=40), nullable=True),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_fetch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("kind IN ('telegram', 'rss')", name=op.f("ck_content_sources_kind")),
        sa.CheckConstraint("length(ref) BETWEEN 1 AND 512", name=op.f("ck_content_sources_ref_length")),
        sa.CheckConstraint("length(label) BETWEEN 1 AND 80", name=op.f("ck_content_sources_label_length")),
        sa.CheckConstraint("failures >= 0 AND last_count >= 0", name=op.f("ck_content_sources_counts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_sources")),
        sa.UniqueConstraint("kind", "ref", name="uq_content_sources_kind_ref"),
    )
    op.create_table(
        "content_candidates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_label", sa.String(length=80), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("views", sa.BigInteger(), nullable=True),
        sa.Column("source_text", sa.Text(), server_default="", nullable=False),
        sa.Column("norm_text", sa.Text(), server_default="", nullable=False),
        sa.Column("title", sa.String(length=120), server_default="", nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("duplicate_of", sa.BigInteger(), nullable=True),
        sa.Column("media_kind", sa.String(length=8), nullable=True),
        sa.Column("media_url", sa.String(length=2048), nullable=True),
        sa.Column("thumb_url", sa.String(length=2048), nullable=True),
        sa.Column("media_state", sa.String(length=8), server_default="none", nullable=False),
        sa.Column("media_error", sa.String(length=40), nullable=True),
        sa.Column("media_tries", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'used', 'expired', 'duplicate')",
            name=op.f("ck_content_candidates_status"),
        ),
        sa.CheckConstraint(
            "source_kind IN ('telegram', 'rss')", name=op.f("ck_content_candidates_source_kind")
        ),
        sa.CheckConstraint(
            "media_kind IS NULL OR media_kind IN ('photo', 'video')",
            name=op.f("ck_content_candidates_media_kind"),
        ),
        sa.CheckConstraint(
            "media_state IN ('none', 'waiting', 'ready', 'failed')",
            name=op.f("ck_content_candidates_media_state"),
        ),
        sa.CheckConstraint("views IS NULL OR views >= 0", name=op.f("ck_content_candidates_views")),
        sa.CheckConstraint(
            "length(title) <= 120 AND length(body) <= 2000",
            name=op.f("ck_content_candidates_caption_length"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["content_sources.id"],
            name=op.f("fk_content_candidates_source_id_content_sources"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of"],
            ["content_candidates.id"],
            name=op.f("fk_content_candidates_duplicate_of_content_candidates"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_candidates")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_content_candidates_source_external"),
    )
    op.create_index(
        "ix_content_candidates_status_created", "content_candidates", ["status", "created_at"], unique=False
    )
    op.create_table(
        "content_media",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("mime", sa.String(length=20), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('main', 'thumb')", name=op.f("ck_content_media_role")),
        sa.CheckConstraint(
            "mime IN ('image/jpeg', 'image/png', 'video/mp4')", name=op.f("ck_content_media_mime")
        ),
        sa.CheckConstraint("size > 0 AND size = octet_length(data)", name=op.f("ck_content_media_size")),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["content_candidates.id"],
            name=op.f("fk_content_media_candidate_id_content_candidates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_media")),
        sa.UniqueConstraint("candidate_id", "role", name="uq_content_media_candidate_role"),
    )
    op.create_index("ix_content_media_sha256", "content_media", ["sha256"], unique=False)
    op.add_column("posts", sa.Column("content_candidate_id", sa.BigInteger(), nullable=True))
    op.add_column("posts", sa.Column("media_kind", sa.String(length=8), nullable=True))
    op.create_foreign_key(
        op.f("fk_posts_content_candidate_id_content_candidates"),
        "posts",
        "content_candidates",
        ["content_candidate_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_posts_media_kind"), "posts", "media_kind IS NULL OR media_kind IN ('photo', 'video')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_posts_media_kind"), "posts", type_="check")
    op.drop_constraint(op.f("fk_posts_content_candidate_id_content_candidates"), "posts", type_="foreignkey")
    op.drop_column("posts", "media_kind")
    op.drop_column("posts", "content_candidate_id")
    op.drop_index("ix_content_media_sha256", table_name="content_media")
    op.drop_table("content_media")
    op.drop_index("ix_content_candidates_status_created", table_name="content_candidates")
    op.drop_table("content_candidates")
    op.drop_table("content_sources")

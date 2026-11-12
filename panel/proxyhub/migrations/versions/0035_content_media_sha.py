"""a candidate's photo keeps its sha256 after the file is gone (the «عکس یکسان» gate, D52)

* ``content_candidates.media_sha256``: the sha256 of the candidate's photo, written when it is
  downloaded. The file of a posted candidate goes an hour after its post; the hash stays with the
  row, so the same photo from another source is still found to be a duplicate for 48 hours.
  Filled here from the photos still stored.

Revision ID: 0035_content_media_sha
Revises: 0034_api_admin_devices
Create Date: 2026-11-12 10:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_content_media_sha"
down_revision = "0034_api_admin_devices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_candidates", sa.Column("media_sha256", sa.String(length=64), nullable=True))
    op.create_index("ix_content_candidates_media_sha256", "content_candidates", ["media_sha256"])
    op.execute(
        "UPDATE content_candidates AS c SET media_sha256 = m.sha256 FROM content_media AS m "
        "WHERE m.candidate_id = c.id AND m.role = 'main' AND c.media_kind = 'photo'"
    )


def downgrade() -> None:
    op.drop_index("ix_content_candidates_media_sha256", table_name="content_candidates")
    op.drop_column("content_candidates", "media_sha256")

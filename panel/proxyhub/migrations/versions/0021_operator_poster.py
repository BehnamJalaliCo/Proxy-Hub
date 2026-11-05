"""operator poster: the operator account's own outbox jobs (decision D38)

``outbox.kind`` may also be ``operator_send_post`` and ``operator_edit_post``: a post or a status
edit that the operator account (a Premium user account, proxyhub/posts/operator.py) sends with
animated custom emoji. The bot's outbox loop never picks them; a post the operator could not send
is handed over to the bot by turning its job into ``send_post``. Still never a delete job (rule 6).

Downgrade: an operator job that is still waiting is finished as failed (the older code cannot send
it), and every operator job takes the bot's kind of the same meaning, so the old constraint holds.

Revision ID: 0021_operator_poster
Revises: 0020_merge
Create Date: 2026-11-05 18:00:00
"""

from alembic import op

revision = "0021_operator_poster"
down_revision = "0020_merge"
branch_labels = None
depends_on = None

OLD_KINDS = "kind IN ('send_post', 'edit_post', 'create_invite_link', 'alert')"
NEW_KINDS = (
    "kind IN ('send_post', 'edit_post', 'create_invite_link', 'alert', "
    "'operator_send_post', 'operator_edit_post')"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_outbox_kind"), "outbox", type_="check")
    op.create_check_constraint(op.f("ck_outbox_kind"), "outbox", NEW_KINDS)


def downgrade() -> None:
    op.execute(
        "UPDATE outbox SET done_at = now(), last_error = 'failed: operator job (downgrade)' "
        "WHERE kind IN ('operator_send_post', 'operator_edit_post') AND done_at IS NULL"
    )
    op.execute("UPDATE outbox SET kind = 'send_post' WHERE kind = 'operator_send_post'")
    op.execute("UPDATE outbox SET kind = 'edit_post' WHERE kind = 'operator_edit_post'")
    op.drop_constraint(op.f("ck_outbox_kind"), "outbox", type_="check")
    op.create_check_constraint(op.f("ck_outbox_kind"), "outbox", OLD_KINDS)

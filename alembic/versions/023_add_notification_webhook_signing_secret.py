"""add notification webhook signing secret

Adds an optional HMAC signing secret for the webhook notification channel. When
configured, every webhook POST body is signed with HMAC-SHA256 and the digest is
sent in an ``X-Signature-256`` header so receivers can verify authenticity; when
absent, webhooks stay unsigned.

* New ``notification_settings.webhook_signing_secret`` column (nullable String).
  The value is encrypted at rest by the settings service exactly like the SMTP
  password, and is never returned in plaintext from the display path.

The column is additive and nullable, so every pre-existing row reads back as
``NULL`` (no signing secret, webhooks unsigned), preserving today's behaviour.

Revision ID: 023_add_notification_webhook_signing_secret
Revises: 022_add_project_event_triggers
Create Date: 2026-06-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from timelapse_manager.db.migration_utils import suspend_foreign_keys

# revision identifiers, used by Alembic.
revision: str = "023_add_notification_webhook_signing_secret"
down_revision: str | None = "022_add_project_event_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("notification_settings") as batch_op:
        batch_op.add_column(
            sa.Column("webhook_signing_secret", sa.String(), nullable=True)
        )


def downgrade() -> None:
    # Dropping a column makes SQLite rebuild ``notification_settings``
    # (create-copy-drop-rename). The implicit DROP of the old table cascades
    # through any ON DELETE CASCADE foreign keys and would silently delete child
    # rows. Suspend FK enforcement across the rebuild so child data survives.
    with (
        suspend_foreign_keys(op),
        op.batch_alter_table("notification_settings") as batch_op,
    ):
        batch_op.drop_column("webhook_signing_secret")

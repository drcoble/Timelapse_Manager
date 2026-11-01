"""secrets key record

Issue #67 (S8): nothing recorded which key encrypted the stored secrets, so a
missing, restored or rotated key either minted a new key silently (and every
stored credential became undecryptable) or surfaced only later as a per-use
failure. A new singleton table ``secrets_meta`` records the key's provider and
fingerprint so startup can check the key it loaded against the key that wrote
the data:

* ``provider``: ``file`` or ``os_keystore``, the store that holds the key;
* ``key_fingerprint``: a keyed hash of the key (never the key itself);
* ``created_at`` / ``rotated_at``: when the row was first written and when the
  key was last rotated.

The table starts empty. On the first boot after the upgrade the application
checks the current key against the stored ciphertext and, when it decrypts,
records the row, so an upgrade never locks anyone out.

A new table only: no existing table is rebuilt and no row is touched, so no
foreign key can cascade. Mirrored by ``SecretsMeta``.

Revision ID: 040_secrets_meta
Revises: 039_camera_tls_pinned_cert
Create Date: 2026-10-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "040_secrets_meta"
down_revision: str | None = "039_camera_tls_pinned_cert"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secrets_meta",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("key_fingerprint", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_secrets_meta_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("secrets_meta")

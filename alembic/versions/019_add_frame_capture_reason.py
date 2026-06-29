"""add frame capture-reason provenance

Records, per frame, *why* it was captured on the unified one-shot capture path:

* New ``frame.capture_reason`` column (nullable String) -- ``NULL`` for an
  ordinary interval capture or an uploaded frame; a short reason token (e.g.
  ``"anchor:clock"``, ``"anchor:solar_noon"``, ``"event:<topic>"``) when the
  frame was produced by an exact-time anchor or an event trigger.

The column is additive and nullable, so every pre-existing row reads back as
``NULL`` (no recorded reason), preserving today's behaviour.

Revision ID: 019_add_frame_capture_reason
Revises: 018_add_frame_capture_timestamp_inferred
Create Date: 2026-06-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "019_add_frame_capture_reason"
down_revision: str | None = "018_add_frame_capture_timestamp_inferred"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.add_column(sa.Column("capture_reason", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.drop_column("capture_reason")

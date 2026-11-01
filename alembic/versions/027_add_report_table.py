"""add report table

Issue #8: a user-initiated "Report a problem" stores each submission as a row
plus a redacted diagnostic zip under ``<data_dir>/reports``.

* New ``report`` table:
  - ``report_key`` -- ``TLM-YYYYMMDD-NNNN``, unique.
  - ``user_id`` / ``project_id`` / ``camera_id`` -- nullable foreign keys,
    ``ON DELETE SET NULL`` so deleting a user, project or camera keeps the
    report; ``username`` keeps the reporter's name for the list.
  - ``area``, ``title``, ``description``, ``reported_at`` (when the problem
    happened, naive UTC) and ``created_at``.
  - ``zip_path`` (file name under the reports directory) and ``zip_size_bytes``.
  - ``delivery_state`` (``pending``/``ok``/``failed`` roll-up, indexed for the
    admin list) and ``deliveries`` (JSON, per destination status and last
    error).
  - ``gitea_issue_number`` / ``gitea_issue_url`` once an issue is created.
  - Indexes on ``(user_id, created_at)`` for the rate limit, ``created_at`` for
    retention and listing, and ``delivery_state``.

Nothing references ``report``, so the downgrade is a plain ``drop_table``. The
zip files under the reports directory are left on disk.

Revision ID: 027_add_report_table
Revises: 026_add_render_job_frame_window
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "027_add_report_table"
down_revision: str | None = "026_add_render_job_frame_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_key", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("area", sa.String(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("camera_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("reported_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("zip_path", sa.String(), nullable=True),
        sa.Column("zip_size_bytes", sa.Integer(), nullable=True),
        sa.Column("delivery_state", sa.String(), nullable=False),
        sa.Column("deliveries", sa.JSON(), nullable=True),
        sa.Column("gitea_issue_number", sa.Integer(), nullable=True),
        sa.Column("gitea_issue_url", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["camera_id"], ["camera.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_key", name="uq_report_report_key"),
    )
    op.create_index(
        "ix_report_user_id_created_at", "report", ["user_id", "created_at"]
    )
    op.create_index("ix_report_created_at", "report", ["created_at"])
    op.create_index("ix_report_delivery_state", "report", ["delivery_state"])


def downgrade() -> None:
    op.drop_index("ix_report_delivery_state", table_name="report")
    op.drop_index("ix_report_created_at", table_name="report")
    op.drop_index("ix_report_user_id_created_at", table_name="report")
    op.drop_table("report")

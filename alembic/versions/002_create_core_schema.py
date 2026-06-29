"""create core schema

Creates the core entity tables: cameras, projects, frames, render jobs,
milestones, users, sessions, the two single-row settings tables, and the event
log. Foreign keys, unique constraints, single-row check constraints, and the
supporting indexes are defined here to match the ORM models.

Revision ID: 002_create_core_schema
Revises: 001_empty_baseline
Create Date: 2026-06-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_create_core_schema"
down_revision: str | None = "001_empty_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, name: str) -> sa.Enum:
    """Build a portable (CHECK-constraint) enum type matching the models."""
    return sa.Enum(*values, name=name, native_enum=False)


def upgrade() -> None:
    op.create_table(
        "camera",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("address", sa.String(), nullable=True),
        sa.Column(
            "protocol",
            _enum("onvif", "rtsp", "http", "vapix", name="camera_protocol"),
            nullable=True,
        ),
        sa.Column("credentials", sa.JSON(), nullable=True),
        sa.Column("snapshot_uri", sa.String(), nullable=True),
        sa.Column("stream_uri", sa.String(), nullable=True),
        sa.Column("default_resolution", sa.String(), nullable=True),
        sa.Column("geolocation_latitude", sa.Float(), nullable=True),
        sa.Column("geolocation_longitude", sa.Float(), nullable=True),
        sa.Column(
            "geolocation_source",
            _enum("camera", "manual", name="camera_geolocation_source"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column(
            "auth_source",
            _enum("local", "ldap", name="user_auth_source"),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column(
            "role",
            _enum("admin", "operator", "viewer", name="user_role"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "project",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("camera_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("capture_interval_seconds", sa.Integer(), nullable=True),
        sa.Column("schedule", sa.JSON(), nullable=True),
        sa.Column("render_schedule", sa.JSON(), nullable=True),
        sa.Column("archive_schedule", sa.JSON(), nullable=True),
        sa.Column("post_render_actions", sa.JSON(), nullable=True),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column(
            "operational_status",
            _enum(
                "idle", "capturing", "rendering", "error",
                name="project_operational_status",
            ),
            nullable=False,
        ),
        sa.Column(
            "lifecycle_state",
            _enum("active", "archived", name="project_lifecycle_state"),
            nullable=False,
        ),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["camera_id"], ["camera.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_project_camera_id", "project", ["camera_id"])

    op.create_table(
        "frame",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("sequence_index", sa.Integer(), nullable=False),
        sa.Column("capture_timestamp", sa.DateTime(), nullable=True),
        sa.Column("file_path", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=True),
        sa.Column(
            "capture_status",
            _enum("pending", "captured", "failed", name="frame_capture_status"),
            nullable=False,
        ),
        sa.Column(
            "origin",
            _enum("captured", "uploaded", name="frame_origin"),
            nullable=False,
        ),
        sa.Column(
            "lifecycle_state",
            _enum("active", "soft_deleted", name="frame_lifecycle_state"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "sequence_index", name="uq_frame_project_seq"
        ),
    )
    op.create_index("ix_frame_project_id", "frame", ["project_id"])
    op.create_index("ix_frame_capture_timestamp", "frame", ["capture_timestamp"])

    op.create_table(
        "render_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("encoder_engine", sa.String(), nullable=False),
        sa.Column(
            "kind",
            _enum("manual", "scheduled", "archive", name="render_job_kind"),
            nullable=False,
        ),
        sa.Column("output_settings", sa.JSON(), nullable=True),
        sa.Column("chapters", sa.JSON(), nullable=True),
        sa.Column("browser_streamable", sa.Boolean(), nullable=True),
        sa.Column("overlay_config", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            _enum(
                "pending", "encoding", "done", "failed", name="render_job_status"
            ),
            nullable=False,
        ),
        sa.Column("output_file_path", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_render_job_project_id", "render_job", ["project_id"])

    op.create_table(
        "milestone",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("position_frame_index", sa.Integer(), nullable=True),
        sa.Column("position_timestamp", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_milestone_project_id", "milestone", ["project_id"])

    op.create_table(
        "session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("persistent", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_revalidated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_session_user_id", "session", ["user_id"])

    op.create_table(
        "ldap_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("server_urls", sa.JSON(), nullable=True),
        sa.Column(
            "tls_mode",
            _enum("none", "ldaps", "starttls", name="ldap_tls_mode"),
            nullable=False,
        ),
        sa.Column("bind_dn", sa.String(), nullable=True),
        sa.Column("bind_password", sa.String(), nullable=True),
        sa.Column("search_base", sa.String(), nullable=True),
        sa.Column("search_filter", sa.String(), nullable=True),
        sa.Column("username_attribute", sa.String(), nullable=True),
        sa.Column("display_name_attribute", sa.String(), nullable=True),
        sa.Column("nested_groups", sa.Boolean(), nullable=False),
        sa.Column("admin_group_dn", sa.String(), nullable=True),
        sa.Column("admin_group_filter", sa.String(), nullable=True),
        sa.Column("operator_group_dn", sa.String(), nullable=True),
        sa.Column("operator_group_filter", sa.String(), nullable=True),
        sa.Column("viewer_group_dn", sa.String(), nullable=True),
        sa.Column("viewer_group_filter", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_ldap_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "notification_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("enabled_channels", sa.JSON(), nullable=True),
        sa.Column("smtp_server", sa.String(), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column(
            "smtp_security",
            _enum("none", "tls", "starttls", name="notification_smtp_security"),
            nullable=False,
        ),
        sa.Column("smtp_username", sa.String(), nullable=True),
        sa.Column("smtp_password", sa.String(), nullable=True),
        sa.Column("smtp_from_address", sa.String(), nullable=True),
        sa.Column("smtp_recipients", sa.JSON(), nullable=True),
        sa.Column("webhook_urls", sa.JSON(), nullable=True),
        sa.Column("routing_rules", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_notification_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "scope",
            _enum("system", "camera", "project", name="event_scope"),
            nullable=False,
        ),
        sa.Column("scope_id", sa.Integer(), nullable=True),
        sa.Column(
            "level",
            _enum(
                "debug", "info", "warning", "error", "critical",
                name="event_level",
            ),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["user.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_scope_timestamp", "event", ["scope", "timestamp"])
    op.create_index("ix_event_actor_user_id", "event", ["actor_user_id"])


def downgrade() -> None:
    # Drop in reverse dependency order so foreign-key references are removed
    # before the tables they point at.
    op.drop_index("ix_event_actor_user_id", table_name="event")
    op.drop_index("ix_event_scope_timestamp", table_name="event")
    op.drop_table("event")

    op.drop_table("notification_settings")
    op.drop_table("ldap_settings")

    op.drop_index("ix_session_user_id", table_name="session")
    op.drop_table("session")

    op.drop_index("ix_milestone_project_id", table_name="milestone")
    op.drop_table("milestone")

    op.drop_index("ix_render_job_project_id", table_name="render_job")
    op.drop_table("render_job")

    op.drop_index("ix_frame_capture_timestamp", table_name="frame")
    op.drop_index("ix_frame_project_id", table_name="frame")
    op.drop_table("frame")

    op.drop_index("ix_project_camera_id", table_name="project")
    op.drop_table("project")

    op.drop_table("user")
    op.drop_table("camera")

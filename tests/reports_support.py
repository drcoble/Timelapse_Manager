"""Shared helpers for the problem-report tests (#8).

``seed_secrets`` plants every secret the issue names -- the LDAP bind
password, the SMTP password, the webhook signing secret and webhook URL, camera
credentials (stored, in an RTSP URL, in an error), the default camera
password, a config-file camera password, the Gitea token -- into the database,
the config and the log, and returns them. ``assert_no_secrets`` opens a report
zip and scans the raw bytes of every member for each one.
"""

from __future__ import annotations

import io
import logging
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from timelapse_manager.db.models import (
    Camera,
    Event,
    ExactTimeFire,
    Frame,
    NotificationSettings,
    Project,
    RenderJob,
)
from timelapse_manager.monitoring import settings_service as notification_settings
from timelapse_manager.security import camera_defaults_service, ldap_settings_service
from timelapse_manager.security.crypto import encrypt_credentials, encrypt_secret

LDAP_BIND_PASSWORD = "Ldap-Bind-S3cret-7731"
SMTP_PASSWORD = "Smtp-Relay-S3cret-4410"
WEBHOOK_TOKEN = "hook-token-S3cret-9902"
WEBHOOK_PATH_SECRET = "T0PATHsecretXYZ123"
WEBHOOK_SIGNING_SECRET = "Sign-S3cret-5521"
CAMERA_PASSWORD = "Cam-Stored-S3cret-1187"
CAMERA_URI_PASSWORD = "Cam-Uri-S3cret-2290"
DEFAULT_CAMERA_PASSWORD = "Cam-Default-S3cret-3381"
CONFIG_CAMERA_PASSWORD = "Cfg-Cam-S3cret-6640"
GITEA_TOKEN = "gitea-token-S3cret-0001aa"
POST_ACTION_SECRET = "PostAction-S3cret-8812"

DB_SECRETS = (
    LDAP_BIND_PASSWORD,
    SMTP_PASSWORD,
    WEBHOOK_TOKEN,
    WEBHOOK_PATH_SECRET,
    WEBHOOK_SIGNING_SECRET,
    CAMERA_PASSWORD,
    CAMERA_URI_PASSWORD,
    DEFAULT_CAMERA_PASSWORD,
    POST_ACTION_SECRET,
)


def seed_database_secrets(session: Session) -> int:
    """Plant the database-held secrets plus a project with history; return its id."""
    ldap_settings_service.update_settings(
        session,
        ldap_settings_service.LdapSettingsUpdate(
            enabled=True,
            server_urls=["ldaps://auth.example.test"],
            tls_mode="ldaps",
            tls_ca_cert_path=None,
            bind_dn="uid=bind,dc=example,dc=test",
            bind_password=LDAP_BIND_PASSWORD,
            search_base="dc=example,dc=test",
            search_filter=None,
            group_search_base=None,
            username_attribute=None,
            display_name_attribute=None,
            membership_mode="memberof",
            nested_groups=False,
            admin_group_dn=None,
            admin_group_filter=None,
            operator_group_dn=None,
            operator_group_filter=None,
            viewer_group_dn=None,
            viewer_group_filter=None,
        ),
    )
    notification_settings.update_settings(
        session,
        notification_settings.NotificationSettingsUpdate(
            enabled_channels=["email", "webhook"],
            smtp_server="smtp.example.test",
            smtp_port=587,
            smtp_security="starttls",
            smtp_username="relay",
            smtp_password=SMTP_PASSWORD,
            smtp_from_address="tlm@example.test",
            smtp_recipients=["ops@example.test"],
            webhook_urls=[
                f"https://hooks.example.test/services/{WEBHOOK_PATH_SECRET}"
                f"?token={WEBHOOK_TOKEN}"
            ],
            routing_rules=[],
        ),
    )
    row = session.get(NotificationSettings, 1)
    assert row is not None
    row.webhook_signing_secret = encrypt_secret(WEBHOOK_SIGNING_SECRET)
    camera_defaults_service.update_settings(
        session,
        camera_defaults_service.CameraDefaultsUpdate(
            enabled=True, username="cam-default", password=DEFAULT_CAMERA_PASSWORD
        ),
    )
    camera = Camera(
        name="Roof cam",
        address="10.0.0.5",
        protocol="vapix",
        credentials=encrypt_credentials(
            {"username": "admin", "password": CAMERA_PASSWORD}
        ),
        stream_uri=f"rtsp://admin:{CAMERA_URI_PASSWORD}@10.0.0.5/stream",
    )
    session.add(camera)
    session.flush()
    project = Project(
        camera_id=camera.id,
        name="Roof",
        lifecycle_state="active",
        operational_status="idle",
        exact_time_anchors=[
            {"id": "noon", "kind": "solar_noon", "enabled": True},
        ],
        schedule={"windows": []},
        post_render_actions=[
            {
                "type": "webhook",
                "url": f"https://hooks.example.test/p/{POST_ACTION_SECRET}",
            }
        ],
    )
    session.add(project)
    session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    for index in range(3):
        session.add(
            Frame(
                project_id=project.id,
                sequence_index=index + 1,
                capture_timestamp=now - timedelta(minutes=index),
                file_path=f"/frames/roof/{index}.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
        )
    session.add(
        ExactTimeFire(
            project_id=project.id,
            anchor_id="noon",
            local_date=f"{now:%Y-%m-%d}",
            status="failed",
            fired_at=now - timedelta(hours=1),
            detail=f"capture failed: rtsp://admin:{CAMERA_URI_PASSWORD}@10.0.0.5/",
        )
    )
    session.add(
        RenderJob(
            project_id=project.id, encoder_engine="ffmpeg", kind="manual", status="done"
        )
    )
    session.add(
        Event(
            scope="project",
            scope_id=project.id,
            level="error",
            message=f"camera said: password={SMTP_PASSWORD}",
            timestamp=now,
        )
    )
    session.flush()
    return int(project.id)


def log_secrets() -> None:
    """Put plain-text secrets into the app log (and so the ring buffer)."""
    log = logging.getLogger("timelapse_manager.tests.reports")
    log.warning("stream failed rtsp://admin:%s@10.0.0.5/", CAMERA_URI_PASSWORD)
    log.warning("bind refused with %s", LDAP_BIND_PASSWORD)


def zip_members(data: bytes) -> dict[str, bytes]:
    """Every member of a zip, by name, as raw bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def assert_no_secrets(data: bytes, secrets: Iterable[str]) -> dict[str, bytes]:
    """Assert no secret appears in the raw bytes of any member; return members."""
    members = zip_members(data)
    assert members, "the bundle is empty"
    for name, blob in members.items():
        for secret in secrets:
            if not secret:
                continue
            assert secret.encode("utf-8") not in blob, f"{secret!r} leaked in {name}"
    return members


def file_secret(path: Path | None) -> str | None:
    """The stripped contents of a secret file, if it exists."""
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip()


# A minimal valid PNG (1x1, transparent).
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
    b"\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00"
    b"\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)

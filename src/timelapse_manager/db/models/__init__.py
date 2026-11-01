"""Persistence models.

Importing this package registers every model on the shared declarative
:class:`Base` metadata, so importing it is enough to make the full schema known
to SQLAlchemy (for tooling, reflection, or tests).
"""

from __future__ import annotations

from .base import Base
from .camera import Camera
from .camera_default_credentials import CameraDefaultCredentials
from .event import Event
from .exact_time_fire import ExactTimeFire
from .frame import Frame
from .ldap_settings import LdapSettings
from .login_attempt import LoginAttempt
from .milestone import Milestone
from .notification_cursor import NotificationCursor
from .notification_settings import NotificationSettings
from .project import Project
from .render_job import RenderJob
from .report import Report
from .secrets_meta import SecretsMeta
from .session import Session
from .ssrf_settings import SsrfSettings
from .user import User

__all__ = [
    "Base",
    "Camera",
    "CameraDefaultCredentials",
    "Event",
    "ExactTimeFire",
    "Frame",
    "LdapSettings",
    "LoginAttempt",
    "Milestone",
    "NotificationCursor",
    "NotificationSettings",
    "Project",
    "RenderJob",
    "Report",
    "SecretsMeta",
    "Session",
    "SsrfSettings",
    "User",
]

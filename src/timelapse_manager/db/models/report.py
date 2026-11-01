"""Problem report: one user-filed "Report a problem" submission (#8).

A report pairs what the user wrote with a redacted diagnostic snapshot taken at
submit time. The snapshot itself lives on disk as a zip under
``<data_dir>/reports``; this row is the index admins list, download, resend and
delete from, plus the per-destination delivery record.

Delivery is tracked two ways. ``deliveries`` holds the detail, one entry per
destination (``local``/``email``/``webhook``/``gitea``) with its status, time
and last error. ``delivery_state`` is the roll-up the admin list sorts and
filters on (``pending`` while a delivery is in flight, ``failed`` when any
configured destination failed, ``ok`` otherwise), so "show me what failed" is a
plain indexed query rather than a JSON scan.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Report(Base):
    """A filed problem report and where it was delivered.

    Rows are written once at submit time and afterwards only their delivery
    columns change. The reporter, project and camera links are ``SET NULL`` on
    delete: a report about a project that was later deleted is still worth
    keeping until retention prunes it.
    """

    __tablename__ = "report"
    __table_args__ = (
        # The rate limit counts one user's reports in the last hour.
        Index("ix_report_user_id_created_at", "user_id", "created_at"),
        # Retention prunes oldest-first; the admin list reads newest-first.
        Index("ix_report_created_at", "created_at"),
        Index("ix_report_delivery_state", "delivery_state"),
        # AUTOINCREMENT (migration 029): a deleted report's id is never reused.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ``TLM-YYYYMMDD-NNNN``: the date the report was filed (UTC) and a per-day
    # sequence. Shown to the reporter and used in file and issue names.
    report_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    # The reporter's username at submit time, kept so the list still says who
    # filed a report after the account is deleted.
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    area: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("project.id", ondelete="SET NULL"), nullable=True
    )
    camera_id: Mapped[int | None] = mapped_column(
        ForeignKey("camera.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False, default="")
    # When the user says the problem happened (naive UTC). Defaults to the
    # submit time; the log excerpt is centred on it.
    reported_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    # The zip's file name under ``<data_dir>/reports`` (never an absolute path,
    # so the state directory can move) and its size in bytes.
    zip_path: Mapped[str | None] = mapped_column(String, nullable=True)
    zip_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ``pending`` / ``ok`` / ``failed``; see the module docstring.
    delivery_state: Mapped[str] = mapped_column(
        String, nullable=False, default="pending"
    )
    # Per destination: {"status": "ok"|"failed"|"pending"|"skipped", "at": ISO,
    # "error": short class-or-status text, ...}. Never holds a URL or token.
    deliveries: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # The Gitea issue created for this report, once one exists.
    gitea_issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gitea_issue_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Filed anonymized (#21, migration 030): the stored zip, title and
    # description are the anonymized ones, so the reporter may download the
    # zip too. Every report is anonymized while the GitHub hand-off is on.
    anonymized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

"""User-initiated problem reports (#8).

A user who notices something wrong files a report from any page. The app
captures a redacted diagnostic snapshot at that moment, stores it as a zip
under ``<data_dir>/reports`` with a ``report`` row, and delivers it to the
configured destinations (email, a webhook, a Gitea issue).

* :mod:`.store` -- keys, input validation, rate limit, retention, files.
* :mod:`.snapshot` -- the section builders and the zip bundle.
* :mod:`.redaction` -- the value-based final scrub.
* :mod:`.service` -- :func:`file_report`, the shared filing entry point.
* :mod:`.delivery` -- email / webhook / Gitea delivery.

See ``docs/operations/problem-reports.md``.
"""

from __future__ import annotations

from .delivery import deliver_report
from .service import (
    FiledReport,
    Reporter,
    Submission,
    configured_destinations,
    file_report,
)
from .snapshot import (
    SNAPSHOT_SUMMARY,
    RequestContext,
    SnapshotSources,
    capture_live_diagnostics,
)
from .store import (
    AREAS,
    DESTINATIONS,
    Attachment,
    ReportRateLimitedError,
    ReportTooLargeError,
    prune_reports,
    validate_attachment,
)

__all__ = [
    "AREAS",
    "DESTINATIONS",
    "SNAPSHOT_SUMMARY",
    "Attachment",
    "FiledReport",
    "ReportRateLimitedError",
    "ReportTooLargeError",
    "Reporter",
    "RequestContext",
    "SnapshotSources",
    "Submission",
    "capture_live_diagnostics",
    "configured_destinations",
    "deliver_report",
    "file_report",
    "prune_reports",
    "validate_attachment",
]

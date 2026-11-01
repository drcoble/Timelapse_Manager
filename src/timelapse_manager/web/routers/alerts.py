"""Active-alert routes: the alerts summary partial and clear actions."""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    Response,
)
from sqlalchemy.orm import Session as DbSession

from ...db.models import User
from ...logging import redact_text
from ...monitoring import (
    ALERT_LEVEL_THRESHOLD,
    ActiveAlert,
    clear_alert,
    clear_all_alerts,
    get_active_alerts,
)
from ...security.principal import ensure_sentinel_admin
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    OperatorUser,
    templates,
)
from ._viewmodels import (
    AlertScopeLink,
    _fmt_dt,
    alert_scope_links,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_ALERT_LEVEL_CLASS = {
    "warning": "warning",
    "error": "error",
    "critical": "error",
}

# Client event carrying a rotated CSRF token (handled by ``csrf.js``).
CSRF_ROTATED_EVENT = "tlm:csrf"

# Cap on how many alerts the panel lists at once; the badge still shows the
# full active count regardless of this page size.
_ALERTS_PANEL_LIMIT = 50


@dataclass(frozen=True)
class _AlertView:
    """Display projection of one active alert for the panel."""

    id: int
    level: str
    level_class: str
    message: str
    scope: str
    scope_id: int | None
    timestamp: str | None
    # Raw datetime for timezone-aware display via the localdt template filter.
    timestamp_raw: datetime.datetime | None
    # What the alert is about ("camera Driveway"), and a link to it when the
    # camera or project still exists (#69 CAM-R6: offline alerts are per camera).
    scope_label: str = ""
    scope_url: str | None = None


def _alert_view(alert: ActiveAlert, link: AlertScopeLink | None = None) -> _AlertView:
    """Build an alert view model, mapping the level onto its display class."""
    if link is None:
        suffix = f" #{alert.scope_id}" if alert.scope_id is not None else ""
        link = AlertScopeLink(label=f"{alert.scope}{suffix}", url=None)
    return _AlertView(
        id=alert.id,
        level=alert.level,
        level_class=_ALERT_LEVEL_CLASS.get(alert.level, "warning"),
        message=redact_text(alert.message or ""),
        scope=alert.scope,
        scope_id=alert.scope_id,
        timestamp=_fmt_dt(alert.timestamp),
        timestamp_raw=alert.timestamp,
        scope_label=link.label,
        scope_url=link.url,
    )


def _active_alerts_fragment(request: Request, db: DbSession, user: User) -> Response:
    """Render the active-alerts update for the given user.

    The fragment is out-of-band elements only (#70 F-R6): replacements for
    ``#alerts-badge``, ``#alerts-count-label`` and ``#alerts-panel-body``. The
    toggle and the panel around them are static in the page header, so neither
    a poll nor a clear can take the toggle's focus or close an open panel.

    Shared by the polling summary load and the post-clear refresh so the badge
    count and the panel always reflect the same query. The clear controls are
    gated in the template on ``can_operate`` (supplied by the base context),
    so a viewer sees the list without any dismiss/clear affordances.
    """
    alerts, total = get_active_alerts(
        db,
        level_floor=ALERT_LEVEL_THRESHOLD,
        limit=_ALERTS_PANEL_LIMIT,
        offset=0,
    )
    links = alert_scope_links(db, [(a.scope, a.scope_id) for a in alerts])
    return templates.TemplateResponse(
        request,
        "_partials/active_alerts.html",
        deps.base_context(
            request,
            db,
            user,
            alert_count=total,
            alerts=[_alert_view(a, links.get((a.scope, a.scope_id))) for a in alerts],
        ),
    )


@router.get("/alerts/summary", response_class=HTMLResponse)
def alerts_summary(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Return the out-of-band alerts update for the polling indicator.

    Available to any authenticated user. The badge shows the total active
    count; the clear controls render only for operators and admins.

    Also the page's CSRF rotation signal (#70 F-R3). Every htmx request carries
    the page's token (``csrf.js``), so this poll does too. When that token no
    longer verifies against the live session -- another tab signed in again --
    the response adds ``HX-Trigger: {"tlm:csrf": {"token": ...}}`` with a fresh
    masked token, and ``csrf.js`` writes it into the page's meta tag and every
    form, so open pages keep working without a reload (#165). A current token,
    masked or bare, never triggers.
    """
    response = _active_alerts_fragment(request, db, user)
    if not deps.csrf_presented_is_current(request, db):
        token = deps.csrf_token_for(request, db)
        if token:
            response.headers["HX-Trigger"] = json.dumps(
                {CSRF_ROTATED_EVENT: {"token": token}}
            )
    return response


@router.post("/alerts/{alert_id}/clear", response_class=HTMLResponse)
def alerts_clear_one(
    request: Request, db: DbDep, user: OperatorUser, alert_id: int
) -> Response:
    """Clear one active alert, then return the out-of-band alerts update.

    Operator/admin only (a viewer is 403). Idempotent: clearing an
    already-cleared alert, a non-alert event, or an unknown id is a no-op. The
    underlying event row is never deleted -- only marked cleared.
    """
    ensure_sentinel_admin(db)
    clear_alert(db, event_id=alert_id, user_id=user.id)
    return _active_alerts_fragment(request, db, user)


@router.post("/alerts/clear-all", response_class=HTMLResponse)
def alerts_clear_all(request: Request, db: DbDep, user: OperatorUser) -> Response:
    """Clear every active alert, then return the out-of-band alerts update.

    Operator/admin only (a viewer is 403). Idempotent: with no active alerts
    this clears nothing. Event rows are never deleted -- only marked cleared.
    """
    ensure_sentinel_admin(db)
    clear_all_alerts(db, user_id=user.id)
    return _active_alerts_fragment(request, db, user)

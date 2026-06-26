"""Active-alert routes: the alerts summary partial and clear actions."""

from __future__ import annotations

import datetime
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
    _fmt_dt,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_ALERT_LEVEL_CLASS = {
    "warning": "warning",
    "error": "error",
    "critical": "error",
}

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


def _alert_view(alert: ActiveAlert) -> _AlertView:
    """Build an alert view model, mapping the level onto its display class."""
    return _AlertView(
        id=alert.id,
        level=alert.level,
        level_class=_ALERT_LEVEL_CLASS.get(alert.level, "warning"),
        message=redact_text(alert.message or ""),
        scope=alert.scope,
        scope_id=alert.scope_id,
        timestamp=_fmt_dt(alert.timestamp),
        timestamp_raw=alert.timestamp,
    )


def _active_alerts_fragment(request: Request, db: DbSession, user: User) -> Response:
    """Render the active-alerts fragment (badge + panel) for the given user.

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
    return templates.TemplateResponse(
        request,
        "_partials/active_alerts.html",
        deps.base_context(
            request,
            db,
            user,
            alert_count=total,
            alerts=[_alert_view(a) for a in alerts],
        ),
    )


@router.get("/alerts/summary", response_class=HTMLResponse)
def alerts_summary(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Return the active-alerts fragment for the polling indicator.

    Available to any authenticated user. The badge shows the total active
    count; the clear controls render only for operators and admins.
    """
    return _active_alerts_fragment(request, db, user)


@router.post("/alerts/{alert_id}/clear", response_class=HTMLResponse)
def alerts_clear_one(
    request: Request, db: DbDep, user: OperatorUser, alert_id: int
) -> Response:
    """Clear one active alert, then return the refreshed alerts fragment.

    Operator/admin only (a viewer is 403). Idempotent: clearing an
    already-cleared alert, a non-alert event, or an unknown id is a no-op. The
    underlying event row is never deleted -- only marked cleared.
    """
    ensure_sentinel_admin(db)
    clear_alert(db, event_id=alert_id, user_id=user.id)
    return _active_alerts_fragment(request, db, user)


@router.post("/alerts/clear-all", response_class=HTMLResponse)
def alerts_clear_all(request: Request, db: DbDep, user: OperatorUser) -> Response:
    """Clear every active alert, then return the refreshed alerts fragment.

    Operator/admin only (a viewer is 403). Idempotent: with no active alerts
    this clears nothing. Event rows are never deleted -- only marked cleared.
    """
    ensure_sentinel_admin(db)
    clear_all_alerts(db, user_id=user.id)
    return _active_alerts_fragment(request, db, user)

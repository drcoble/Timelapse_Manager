"""Active-alerts endpoints.

Exposes the in-UI active-alerts surface: list the currently active alerts, clear
one by id, and clear them all. Reads are token-gated (the parent router attaches
the local-token dependency); the two clear mutations additionally require an
operator-or-admin principal and attribute the clear to that actor.

An *active alert* is an uncleared event at or above the alert severity threshold
(see :mod:`timelapse_manager.monitoring.alerts`). Clearing only marks the event
cleared -- it never deletes the row -- and is idempotent: clearing an
already-cleared alert, a non-alert (info-level) event, or an unknown id reports
zero cleared rather than erroring.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db.session import get_session
from ..monitoring import (
    ALERT_LEVEL_THRESHOLD,
    ActiveAlert,
    clear_alert,
    clear_all_alerts,
    get_active_alerts,
)
from ..security import Principal, require_operator_or_admin_principal
from ..security.principal import ensure_sentinel_admin

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertOut(BaseModel):
    """One active alert as returned to clients."""

    id: int
    scope: str
    scope_id: int | None
    level: str
    message: str
    timestamp: str
    event_type: str | None
    reason: str | None


class AlertListOut(BaseModel):
    """The active-alerts list and its total count."""

    alerts: list[AlertOut]
    total: int


class ClearResult(BaseModel):
    """The outcome of a clear operation: how many alerts were cleared."""

    cleared: int


def _alert_out(alert: ActiveAlert) -> AlertOut:
    """Project an :class:`ActiveAlert` snapshot onto its public representation."""
    return AlertOut(
        id=alert.id,
        scope=alert.scope,
        scope_id=alert.scope_id,
        level=alert.level,
        message=alert.message,
        timestamp=alert.timestamp.isoformat(),
        event_type=alert.event_type,
        reason=alert.reason,
    )


@router.get("", response_model=AlertListOut)
def list_alerts(
    session: Annotated[Session, Depends(get_session)],
    scope: str | None = None,
    scope_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> AlertListOut:
    """List the active alerts (newest first) with the total active count.

    Active means uncleared and at or above the alert severity threshold
    (``warning`` by default); info-level events and cleared events are excluded.
    """
    alerts, total = get_active_alerts(
        session,
        scope=scope,
        scope_id=scope_id,
        level_floor=ALERT_LEVEL_THRESHOLD,
        limit=limit,
        offset=offset,
    )
    return AlertListOut(alerts=[_alert_out(a) for a in alerts], total=total)


@router.post("/{alert_id}/clear", response_model=ClearResult)
def clear_one_alert(
    alert_id: int,
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> ClearResult:
    """Clear one active alert by event id, attributed to the principal.

    Idempotent: clearing an already-cleared alert, a non-alert event, or an
    unknown id returns ``cleared = 0`` rather than a 404/error.
    """
    # The clear is attributed via a foreign key to ``user.id``; materialise the
    # sentinel administrator so the attribution holds until real accounts exist.
    ensure_sentinel_admin(session)
    cleared = clear_alert(session, event_id=alert_id, user_id=principal.user_id)
    return ClearResult(cleared=cleared)


@router.post(
    "/clear-all",
    response_model=ClearResult,
    status_code=status.HTTP_200_OK,
)
def clear_all(
    session: Annotated[Session, Depends(get_session)],
    principal: Annotated[Principal, Depends(require_operator_or_admin_principal)],
) -> ClearResult:
    """Clear every active alert, attributed to the principal.

    Idempotent: with no active alerts this returns ``cleared = 0``.
    """
    ensure_sentinel_admin(session)
    cleared = clear_all_alerts(session, user_id=principal.user_id)
    return ClearResult(cleared=cleared)

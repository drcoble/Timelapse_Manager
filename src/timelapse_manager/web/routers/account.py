"""Per-account preference routes: theme, timezone, and password change."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)

from ...security import (
    change_password,
    create_session,
    verify_password,
)
from ...security.sessions import invalidate_request_session, resolve_request_session
from ...security.throttle import password_change_key
from ...services.errors import AppError
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    set_session_cookie,
    templates,
)
from ._shared import _audit, _settings
from .auth import _login_throttle

logger = logging.getLogger(__name__)

router = APIRouter()

# Query-param signals the preferences page renders after a password change. A
# single ``password_error`` code keeps the message wording in the template (and
# never leaks which condition failed beyond the code), while ``password_changed``
# marks the success path. These are read by ``account_preferences.html`` via
# ``request.query_params``.
_PW_ERROR_LDAP = "ldap"
_PW_ERROR_CURRENT = "current"
_PW_ERROR_MISMATCH = "mismatch"
_PW_ERROR_POLICY = "policy"


_VALID_THEMES = frozenset({"light", "dark", "system"})


class TooManyPasswordAttemptsError(AppError):
    """Too many wrong current passwords for one account in the window (#67 S23)."""

    status = 429
    code = "too_many_attempts"


_TOO_MANY_PASSWORD_ATTEMPTS = (
    "Too many incorrect current passwords. Wait a few minutes, then try again."
)

# ``source`` value the Preferences page sends to /account/timezone. Anything
# else is treated as the browser auto-detect, which only fills an empty zone.
_TZ_SOURCE_MANUAL = "manual"


@router.get("/account/preferences", response_class=HTMLResponse)
def account_preferences_page(
    request: Request, db: DbDep, user: CurrentUser
) -> Response:
    """Render the per-user display preferences page (accessible to all roles)."""
    return templates.TemplateResponse(
        request,
        "account_preferences.html",
        deps.base_context(request, db, user),
    )


@router.post("/account/theme")
def account_theme(
    request: Request, db: DbDep, user: CurrentUser, form: FormDep
) -> Response:
    """Persist the authenticated user's theme preference.

    Accepts ``theme`` = ``"light"``, ``"dark"``, or ``"system"``.
    Unknown values are ignored (respond 204 so the JS caller stays silent).
    Returns ``204 No Content`` rather than a redirect because this endpoint
    is called by the nav-bar toggle via ``fetch()``, not a form submission.
    """
    theme = form.get("theme", "")
    if theme in _VALID_THEMES:
        user.theme_preference = theme
        db.flush()
    return Response(status_code=204)


@router.post("/account/timezone")
def account_timezone(
    request: Request, db: DbDep, user: CurrentUser, form: FormDep
) -> Response:
    """Persist the authenticated user's preferred display timezone.

    ``source`` says who is asking (#70 F-R4):

    * ``manual`` -- the Preferences page. The submitted zone replaces the
      stored one; a blank value clears it, so the browser's zone fills it again
      on the next page load. An unknown zone is a 422 with a short ``detail``,
      so the page can say why nothing was saved.
    * ``auto`` (also any other or missing value) -- the browser-detection
      script. It only fills an empty preference: a stored zone, whether chosen
      by hand or detected earlier, wins over the browser's (owner decision D2),
      so a laptop in another zone, or a stale tab, never overwrites it. An
      unknown zone is ignored.

    Returns ``204 No Content`` on success or when ignored -- the callers use
    ``fetch()``.
    """
    tz_name = form.get("timezone", "").strip()
    manual = form.get("source", "") == _TZ_SOURCE_MANUAL
    if manual:
        if not tz_name:
            user.viewer_timezone = None
            db.flush()
            return Response(status_code=204)
        if not deps.is_valid_timezone(tz_name):
            return JSONResponse(
                {
                    "detail": (
                        "Unknown time zone. Use an IANA name such as Europe/London."
                    )
                },
                status_code=422,
            )
        user.viewer_timezone = tz_name
        db.flush()
        return Response(status_code=204)
    if user.viewer_timezone is None and deps.is_valid_timezone(tz_name):
        user.viewer_timezone = tz_name
        db.flush()
    return Response(status_code=204)


def _password_redirect(*, error: str | None = None) -> RedirectResponse:
    """Redirect back to the preferences page carrying a password-change signal.

    On success (``error`` is ``None``) the page shows a confirmation; otherwise
    the error code selects the message to show. The redirect is a 303 so the
    browser re-issues a GET (post/redirect/get), matching the rest of the app's
    form handlers.
    """
    if error is None:
        target = "/account/preferences?password_changed=1"
    else:
        target = f"/account/preferences?password_error={error}"
    return RedirectResponse(url=target, status_code=303)


@router.post("/account/password")
def account_password(
    request: Request, db: DbDep, user: CurrentUser, form: FormDep
) -> Response:
    """Let the signed-in user change their own password.

    Available to every role. Rejects the change for a directory (LDAP) account,
    which carries no local password. Verifies the current password, requires the
    new password to be confirmed and to satisfy the configured minimum length,
    then sets the new hash and revokes every *other* session for the user. The
    browser issuing the change keeps working: a fresh session is minted for it
    (carrying the prior session's persistence) so the user is not signed out of
    the device they are using. Outcomes redirect back to the preferences page
    with a query-param signal; passwords are never logged.

    Guessing the current password from a stolen session is throttled (#67
    S23): each check is reserved in advance against ``account:<user id>``, and
    once ``auth.throttle_max_failures`` wrong ones fall in the window the route
    answers ``429`` without checking. A correct current password releases its
    reservation. These attempts add nothing to the login's per-username delay.
    """
    settings = _settings()

    # A directory account has its credential managed externally; never mutate it.
    if user.auth_source != "local":
        return _password_redirect(error=_PW_ERROR_LDAP)

    current_password = form.get("current_password", "")
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    # The reservation writes on its own connection. Commit first, so a session
    # touch the auth dependency left pending here cannot hold SQLite's single
    # write lock against it. The factory keeps loaded rows readable on commit.
    db.commit()
    throttle = _login_throttle()
    reservation = throttle.try_reserve(
        ip=password_change_key(int(user.id)), username=user.username, backoff=False
    )
    if reservation is None:
        raise TooManyPasswordAttemptsError(_TOO_MANY_PASSWORD_ATTEMPTS)
    if not verify_password(current_password, user.password_hash, settings.auth):
        # The reservation stays: it is this attempt's recorded failure.
        return _password_redirect(error=_PW_ERROR_CURRENT)
    # Before any write on the request session, so it may use its own connection.
    throttle.release(reservation, succeeded=True)

    if new_password != confirm_password:
        return _password_redirect(error=_PW_ERROR_MISMATCH)

    if len(new_password) < settings.auth.password_min_length:
        return _password_redirect(error=_PW_ERROR_POLICY)

    # Capture the current session's persistence before the change revokes it, so
    # the replacement session matches (a "remember me" session stays persistent).
    # The request's single session resolution (#67 R3): the dependency already
    # resolved it, so this adds no lookup.
    resolved = resolve_request_session(request, db, settings=settings.session)
    persistent = resolved.persistent if resolved is not None else False

    # Sets the new hash and revokes ALL of this user's sessions (including the
    # current one), so any previously stolen token dies on a credential change.
    change_password(db, user, new_password, settings=settings.auth)
    # The resolution above describes a session that no longer exists.
    invalidate_request_session(request)

    # Re-establish a session for the browser making the change so it is not
    # logged out; every other session stays revoked.
    _new_row, raw_new = create_session(
        db, user, remember_me=persistent, settings=settings.session
    )
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message=f"password changed by user {user.username!r}",
    )

    response = _password_redirect()
    set_session_cookie(
        response, request, raw_new, settings=settings, persistent=persistent
    )
    return response

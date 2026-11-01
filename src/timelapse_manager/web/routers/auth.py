"""Authentication routes: login, logout, and first-run admin setup."""

from __future__ import annotations

import asyncio
import datetime
import enum
import logging
from dataclasses import dataclass

from fastapi import APIRouter, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from ...config import Settings
from ...db.models import User
from ...runtime import get_context
from ...security import (
    BruteForceThrottle,
    authenticate_user,
    create_initial_admin,
    create_session,
    first_run_needed,
    revoke_session,
    rotate_session,
)
from ...security.authz import session_cookie_value
from ...security.ldap_directory import (
    DETAIL_NO_SERVER_REACHABLE,
    LdapOutcome,
    LdapProvisioningError,
    map_groups_to_role,
    provision_user,
)
from ...security.ldap_directory import (
    authenticate as ldap_authenticate,
)
from ...security.ldap_settings_service import load_settings as load_ldap_settings
from ...security.ldap_settings_service import resolve_bind_password
from ...security.login import (
    discard_setup_code,
    ensure_setup_code,
    first_run_lock,
    verify_setup_code,
)
from ...security.session_revalidation import (
    breaker_for,
    breaker_window_seconds,
    record_directory_result,
)
from ...security.sessions import invalidate_request_session
from ...security.throttle import LoginReservation, throttle_ip_key
from ...usernames import directory_username, normalize_username, username_key
from ...version import get_app_version
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    templates,
)
from ..presession import (
    PRESESSION_RETRY_MESSAGE,
    attach_presession,
    clear_presession,
    presession_for,
    presession_valid,
)
from ..safe_redirect import safe_local_path
from ._shared import (
    _audit,
    _settings,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_throttle: BruteForceThrottle | None = None


def _login_throttle() -> BruteForceThrottle:
    """Return the shared login throttle, building it from settings on first use.

    The password-change route (:mod:`.account`) shares it, so both count in one
    ``login_attempt`` table under keys that cannot collide.
    """
    global _throttle
    if _throttle is None:
        _throttle = BruteForceThrottle(get_context().settings.auth)
    return _throttle


# The delay before a delayed login checks the password. A module attribute so
# tests can replace it; it is awaited on the event loop, holding no worker
# thread, so a flood of delayed logins cannot starve the threadpool.
_backoff_sleep = asyncio.sleep

# Shown when the directory could not be asked (#67 S7). It names no account and
# no reason, and it is shown whether or not a local account holds the name.
DIRECTORY_UNAVAILABLE_MESSAGE = (
    "The directory server could not be reached, so a directory sign-in could not "
    "be checked. Try again in a moment."
)


class LoginOutcome(enum.Enum):
    """How :func:`_resolve_login` ended."""

    AUTHENTICATED = "authenticated"
    # Wrong password, unknown user, no mapped group, a name collision: counted.
    DENIED = "denied"
    # The directory was unreachable or misconfigured before the password
    # reached it. Shown with its own message, and counted like a denial for
    # every name (see :func:`_complete_login`).
    DIRECTORY_UNAVAILABLE = "directory_unavailable"


@dataclass(frozen=True)
class LoginResolution:
    """The result of :func:`_resolve_login`: an outcome and, on success, the user."""

    outcome: LoginOutcome
    user: User | None = None


def _client_ip(request: Request) -> str:
    """Return the request's effective client IP for throttling.

    Reads the reverse-proxy-aware client address resolved by
    :class:`..middleware.EffectiveSchemeMiddleware` and stashed on
    ``request.state``, so a request behind a trusted proxy is throttled by the
    real client rather than the proxy. Falls back to the raw peer address (then a
    stable placeholder) if the middleware has not run, mirroring the defensive
    ``effective_scheme`` read elsewhere.
    """
    effective = getattr(request.state, "effective_client_ip", None)
    if isinstance(effective, str) and effective:
        return effective
    if request.client is not None:
        return request.client.host
    return "unknown"


def _safe_next(raw: str | None) -> str:
    """Return a safe same-origin redirect target, or ``"/"`` if unsafe/empty.

    A ``next`` value arrives from an attacker-influenceable place (a crafted
    ``/login?next=…`` link), so it goes through the shared
    :func:`..safe_redirect.safe_local_path` rule (an absolute path on this origin,
    a navigable page, nothing a browser would rewrite into another host). Anything
    else falls back to the dashboard.
    """
    return safe_local_path(raw) or "/"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: DbDep) -> Response:
    """Render the login page. Redirects to setup if no admin exists yet."""
    if first_run_needed(db):
        return RedirectResponse(url="/first-run", status_code=303)
    next_target = _safe_next(request.query_params.get("next"))
    return _login_page(request, next_target=next_target)


@router.post("/login")
async def login_submit(request: Request, db: DbDep, form: FormDep) -> Response:
    """Authenticate, rotate the session, set the cookie, and redirect.

    A single streamlined form: the server resolves the account's source itself
    (the user never picks "local vs directory"). A local account is tried first;
    if it does not authenticate and directory auth is enabled, the directory is
    tried. On success the session is established identically for both sources.

    On any failure the page re-renders with a single generic error so neither the
    username's existence, the resolved source, nor the failure reason is
    disclosed. A missing field is treated as the same generic failure.

    Throttling (#67 S7), in three steps:

    1. In a worker thread, the attempt is reserved: counted as a failure before
       the password is checked, or refused (the same generic error) when the
       source has used its budget.
    2. A username with repeated recent failures is delayed here, on the event
       loop, holding no worker thread. The delay never refuses.
    3. In a worker thread, :func:`_complete_login` checks the password and keeps
       or releases the reservation.
    """
    next_target = _safe_next(form.get("next"))
    # Before anything that costs: a forged cross-site POST neither spends the
    # victim's throttle budget nor reaches the password hash.
    if not presession_valid(request, form.get("csrf_token")):
        return _login_page(
            request,
            next_target=next_target,
            flash=[{"type": "error", "message": PRESESSION_RETRY_MESSAGE}],
            status_code=status.HTTP_403_FORBIDDEN,
        )
    username = normalize_username(form.get("username", ""))
    password = form.get("password", "")
    if not username or not password:
        return _login_error(request, next_target=next_target)

    throttle = _login_throttle()
    ip = throttle_ip_key(_client_ip(request))
    reservation = await run_in_threadpool(
        throttle.try_reserve, ip=ip, username=username
    )
    if reservation is None:
        return _login_error(request, next_target=next_target)
    if reservation.delay_seconds > 0:
        await _backoff_sleep(reservation.delay_seconds)
    return await run_in_threadpool(
        _complete_login,
        request,
        db,
        throttle=throttle,
        reservation=reservation,
        username=username,
        password=password,
        remember_me=bool(form.get("remember_me")),
        next_target=next_target,
    )


def _complete_login(
    request: Request,
    db: DbSession,
    *,
    throttle: BruteForceThrottle,
    reservation: LoginReservation,
    username: str,
    password: str,
    remember_me: bool,
    next_target: str,
) -> Response:
    """Check the password, settle the reservation, and sign the user in.

    * Success releases the reservation and writes the success row, forgiving
      this source's earlier failures for the name.
    * A denial keeps the reservation: that row is the recorded failure.
    * A directory outage keeps it too, for every name. The local password is
      checked before the directory is asked, so for a local name the attempt
      was a real guess; releasing only for other names would make the throttle
      state reveal which names are local, and releasing for all would allow
      unlimited guessing of the local break-glass account during an outage.
      The page shows the outage message for every name alike.

    Settling rides the request session: provisioning may already have written
    on it, and SQLite allows one writer.
    """
    settings = _settings()
    resolution = _resolve_login(db, username, password, settings=settings)
    if resolution.outcome is LoginOutcome.DIRECTORY_UNAVAILABLE:
        return _login_unavailable(request, next_target=next_target)
    user = resolution.user
    if resolution.outcome is not LoginOutcome.AUTHENTICATED or user is None:
        return _login_error(request, next_target=next_target)

    throttle.release(reservation, succeeded=True, db=db)
    old_token = session_cookie_value(request, settings.session) or ""
    row, raw_token = rotate_session(
        db, old_token, user, remember_me=remember_me, settings=settings.session
    )
    # The CSRF layer may have resolved the old cookie; it was just revoked.
    invalidate_request_session(request)
    # Seed the directory re-evaluation clock for LDAP sessions so a freshly
    # established session is not re-checked until a full interval has elapsed.
    # Local sessions leave it unset; the re-eval path short-circuits on them.
    if user.auth_source == "ldap":
        row.last_revalidated_at = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None
        )
        db.flush()
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message=f"user {user.username!r} signed in",
    )
    response = RedirectResponse(url=next_target, status_code=303)
    deps.set_session_cookie(
        response, request, raw_token, settings=settings, persistent=remember_me
    )
    clear_presession(response, request)
    return response


def _directory_was_unavailable(outcome: LdapOutcome, detail: str) -> bool:
    """Return whether a directory result means the password was never checked.

    True only for faults before the user search, which therefore reveal
    nothing about the submitted name: a configuration fault (no server, no user
    search, a failed service bind) and :data:`DETAIL_NO_SERVER_REACHABLE` (no
    socket could be opened before the search). Everything later gets the
    generic failure page, or the answer would tell names apart:

    * :data:`DETAIL_LOST_AFTER_USER_SEARCH` -- only a name the search found
      reaches the user bind, so an outage there would confirm it exists;
    * any other protocol error ("directory error") -- it may follow a
      successful user bind, so it would tell a correct password from a wrong one.
    """
    if outcome is LdapOutcome.CONFIG_ERROR:
        return True
    return (
        outcome is LdapOutcome.SERVER_UNREACHABLE
        and detail == DETAIL_NO_SERVER_REACHABLE
    )


def _resolve_login(
    db: DbSession, username: str, password: str, *, settings: Settings
) -> LoginResolution:
    """Resolve a login to a :class:`User`, trying local then directory.

    A local account always authenticates locally (``authenticate_user`` filters to
    ``auth_source == "local"``). If no local account matches and directory auth is
    enabled, the directory is consulted; a directory user is provisioned/updated
    just-in-time and assigned the role mapped from its group membership.

    Every failure that verified something (wrong password, unknown user, no
    mapped group, a name collision, directory disabled) is
    :attr:`LoginOutcome.DENIED`, so the caller presents one generic outcome. A
    directory fault before the password reached it is
    :attr:`LoginOutcome.DIRECTORY_UNAVAILABLE`. Directory infrastructure faults
    are logged with an admin-facing diagnostic; credentials are never logged.
    """
    denied = LoginResolution(LoginOutcome.DENIED)
    user = authenticate_user(db, username, password, settings=settings.auth)
    if user is not None:
        return LoginResolution(LoginOutcome.AUTHENTICATED, user)

    ldap_view = load_ldap_settings(db)
    if not ldap_view.enabled:
        # Directory auth off (or never configured): behave exactly as local-only.
        return denied

    # The directory was just found unreachable: say so at once (the same page, for
    # every name) instead of queueing behind another connect timeout. The local
    # password was already checked above, so the break-glass account still works.
    breaker = breaker_for(db)
    if not breaker.allow(breaker_window_seconds()):
        return LoginResolution(LoginOutcome.DIRECTORY_UNAVAILABLE)

    result = ldap_authenticate(
        settings=ldap_view,
        username=username,
        password=password,
        bind_password=resolve_bind_password(db),
    )
    record_directory_result(breaker, result.outcome, result.detail)

    if result.outcome is LdapOutcome.AUTHENTICATED:
        role = map_groups_to_role(
            result.groups,
            admin_group_dn=ldap_view.admin_group_dn or None,
            operator_group_dn=ldap_view.operator_group_dn or None,
            viewer_group_dn=ldap_view.viewer_group_dn or None,
        )
        if role is None:
            # Authenticated but in no mapped group: no access, and crucially not
            # provisioned. Generic denial.
            return denied
        try:
            # Key the account on the directory's own spelling of the login name
            # (#4), not on what was typed.
            provisioned = provision_user(
                db,
                username=directory_username(
                    result.attributes, ldap_view.username_attribute or "", username
                ),
                role=role,
                display_name=result.display_name,
            )
        except LdapProvisioningError:
            # A local account already owns this username; a directory login must
            # never take it over. Generic denial, with an admin-facing note. It
            # stays counted: the local password for the name was wrong.
            logger.warning("LDAP login refused: username collides with a local account")
            return denied
        return LoginResolution(LoginOutcome.AUTHENTICATED, provisioned)

    if result.outcome in (LdapOutcome.SERVER_UNREACHABLE, LdapOutcome.CONFIG_ERROR):
        # Surface a clear admin diagnostic; the user sees only a generic failure.
        logger.warning("LDAP login could not be completed: %s", result.detail)
        if _directory_was_unavailable(result.outcome, result.detail):
            return LoginResolution(LoginOutcome.DIRECTORY_UNAVAILABLE)

    # INVALID_CREDENTIALS / NO_SUCH_USER / DISABLED / a fault that may have
    # followed the bind: generic denial with no enumeration of source or reason.
    return denied


def _login_page(
    request: Request,
    *,
    next_target: str = "/",
    error: bool = False,
    flash: list[dict[str, str]] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Render the login page with a pre-session token in its form.

    Every render of the form carries the token (and sets the cookie it verifies
    against when the browser has none yet), so a retry after any failure posts
    a token the next check accepts.
    """
    presession = presession_for(request)
    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "csrf_token": presession.token(),
            "flash_messages": flash or [],
            "error": error,
            "next": next_target,
            "app_version": get_app_version(),
        },
        status_code=status_code,
    )
    attach_presession(response, request, presession)
    return response


def _login_unavailable(request: Request, *, next_target: str = "/") -> Response:
    """Render the login page saying the directory could not be reached.

    Status 401 like the generic failure: the sign-in did not happen, and a 5xx
    would read as a server fault to the reverse proxy and to monitoring.
    """
    return _login_page(
        request,
        next_target=next_target,
        flash=[{"type": "error", "message": DIRECTORY_UNAVAILABLE_MESSAGE}],
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


def _login_error(request: Request, *, next_target: str = "/") -> Response:
    """Render the login page with the generic invalid-credentials message."""
    return _login_page(
        request,
        next_target=next_target,
        error=True,
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.post("/logout")
def logout(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Revoke the current session and clear the cookie."""
    settings = _settings()
    raw_token = session_cookie_value(request, settings.session) or ""
    revoke_session(db, raw_token)
    invalidate_request_session(request)
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message=f"user {user.username!r} signed out",
    )
    response = RedirectResponse(url="/login", status_code=303)
    deps.clear_session_cookie(response, settings=settings)
    return response


@router.get("/first-run", response_class=HTMLResponse)
def first_run_form(request: Request, db: DbDep) -> Response:
    """Render the first-run setup page, or redirect once setup is complete.

    Creates the one-time setup code file on first use (issue #67, S11); the
    service log names the file. If it cannot be written the page says so, and
    the submit stays closed until it exists.
    """
    if not first_run_needed(db):
        return RedirectResponse(url="/login", status_code=303)
    errors: list[str] = []
    try:
        ensure_setup_code(_settings().paths.data_dir)
    except OSError:
        logger.error("could not write the first-run setup code", exc_info=True)
        errors.append(_SETUP_CODE_UNAVAILABLE)
    return _first_run_page(request, errors=errors, username="")


@router.post("/first-run")
def first_run_submit(request: Request, db: DbDep, form: FormDep) -> Response:
    """Create the initial administrator and sign them in.

    The one-time setup code is checked first: a missing or wrong code is a
    ``403`` before any other field is looked at. Then the password length and
    confirmation are validated; on error the page re-renders with the messages.

    Creation is serialised by :data:`first_run_lock`: inside it the request
    session ends any open read, re-checks that no admin exists, creates the
    admin and its session, and commits, all before the lock is released. Two
    concurrent submits therefore yield exactly one admin; the loser is sent to
    the login page like any submit after setup.
    """
    settings = _settings()
    if not first_run_needed(db):
        return RedirectResponse(url="/login", status_code=303)

    username = normalize_username(form.get("username", ""))
    data_dir = settings.paths.data_dir
    if not presession_valid(request, form.get("csrf_token")):
        return _first_run_page(
            request,
            errors=[PRESESSION_RETRY_MESSAGE],
            username=username,
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not verify_setup_code(data_dir, form.get("setup_code", "")):
        logger.warning("first-run submit refused: setup code missing or wrong")
        return _first_run_page(
            request,
            errors=[_SETUP_CODE_REJECTED],
            username=username,
            status_code=status.HTTP_403_FORBIDDEN,
        )

    password = form.get("password", "")
    password_confirm = form.get("password_confirm", "")
    errors = _validate_new_account(
        username, password, password_confirm, settings=settings
    )
    if errors:
        return _first_run_page(
            request,
            errors=errors,
            username=username,
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    with first_run_lock:
        # End any read transaction this session holds, so the re-check below
        # sees every admin committed by a submit that held the lock before us.
        db.commit()
        if not first_run_needed(db):
            return RedirectResponse(url="/login", status_code=303)
        if db.execute(
            select(User.id).where(User.username_key == username_key(username))
        ).first():
            # Case-insensitive uniqueness (#4), e.g. the reserved ``system``
            # sentinel.
            return _first_run_page(
                request,
                errors=["That username is already taken."],
                username=username,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        admin = create_initial_admin(db, username, password, settings=settings.auth)
        _audit(
            db,
            scope="system",
            scope_id=None,
            actor_user_id=admin.id,
            message=f"initial administrator {admin.username!r} created",
        )
        _, raw_token = create_session(
            db, admin, remember_me=False, settings=settings.session
        )
        db.commit()
    discard_setup_code(data_dir)
    response = RedirectResponse(url="/", status_code=303)
    deps.set_session_cookie(
        response, request, raw_token, settings=settings, persistent=False
    )
    clear_presession(response, request)
    return response


# Shown on the setup page when the code is rejected, or cannot be created. The
# file name and CLI command are given; the full path is only in the service log.
_SETUP_CODE_REJECTED = (
    "The setup code is missing or incorrect. Find it in the setup-code file in "
    "the service's data directory, or run 'timelapse-manager setup-code' on the "
    "server."
)
_SETUP_CODE_UNAVAILABLE = (
    "The setup code could not be created. Check that the service's data "
    "directory is writable, then reload this page."
)


def _first_run_page(
    request: Request,
    *,
    errors: list[str],
    username: str,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """Render the first-run page. The submitted setup code is never echoed."""
    presession = presession_for(request)
    response = templates.TemplateResponse(
        request,
        "first_run.html",
        {
            "request": request,
            "csrf_token": presession.token(),
            "errors": errors,
            "form_values": {"username": username},
            "app_version": get_app_version(),
        },
        status_code=status_code,
    )
    attach_presession(response, request, presession)
    return response


def _validate_new_account(
    username: str, password: str, password_confirm: str, *, settings: Settings
) -> list[str]:
    """Return a list of human-readable validation errors (empty if valid)."""
    errors: list[str] = []
    if not username.strip():
        errors.append("Username is required.")
    if password != password_confirm:
        errors.append("Passwords do not match.")
    if len(password) < settings.auth.password_min_length:
        errors.append(
            f"Password must be at least {settings.auth.password_min_length} characters."
        )
    return errors

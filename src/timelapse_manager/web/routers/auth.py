"""Authentication routes: login, logout, and first-run admin setup."""

from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy.orm import Session as DbSession

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
from ...security.ldap_directory import (
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
from ...version import get_app_version
from .. import dependencies as deps
from ..dependencies import (
    CurrentUser,
    DbDep,
    FormDep,
    templates,
)
from ._shared import (
    _audit,
    _settings,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_throttle: BruteForceThrottle | None = None


def _login_throttle() -> BruteForceThrottle:
    """Return the shared login throttle, building it from settings on first use."""
    global _throttle
    if _throttle is None:
        _throttle = BruteForceThrottle(get_context().settings.auth)
    return _throttle


def _client_ip(request: Request) -> str:
    """Return the request's client IP for throttling, or a stable placeholder."""
    if request.client is not None:
        return request.client.host
    return "unknown"


# Server-rendered fragment/partial endpoints (HTMX poll targets, panel refreshes)
# are not navigable pages: landing on one post-login shows an unstyled partial.
# A ``next`` pointing at one -- whether stale or crafted -- falls back to the
# dashboard. These are GET fragment routes only; the matching POST actions never
# reach ``next``.
_NON_NAVIGABLE_NEXT_PREFIXES = ("/partials/", "/alerts/")


def _safe_next(raw: str | None) -> str:
    """Return a safe same-origin redirect target, or ``"/"`` if unsafe/empty.

    A ``next`` value arrives from an attacker-influenceable place (a crafted
    ``/login?next=…`` link), so it is admitted only when it is an absolute path on
    this origin: it must start with a single ``/`` (not ``//`` or ``/\\``, which
    browsers treat as protocol-relative to another host) and carry no backslashes
    or control characters. It must also be a navigable page, not a fragment
    endpoint. Anything else falls back to the dashboard.
    """
    if not raw or not raw.startswith("/"):
        return "/"
    if raw.startswith(("//", "/\\")):
        return "/"
    if any(c in raw for c in "\\\r\n"):
        return "/"
    if raw.split("?", 1)[0].startswith(_NON_NAVIGABLE_NEXT_PREFIXES):
        return "/"
    return raw


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: DbDep) -> Response:
    """Render the login page. Redirects to setup if no admin exists yet."""
    if first_run_needed(db):
        return RedirectResponse(url="/first-run", status_code=303)
    next_target = _safe_next(request.query_params.get("next"))
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "csrf_token": "",
            "flash_messages": [],
            "error": False,
            "next": next_target,
            "app_version": get_app_version(),
        },
    )


@router.post("/login")
def login_submit(request: Request, db: DbDep, form: FormDep) -> Response:
    """Authenticate, rotate the session, set the cookie, and redirect.

    A single streamlined form: the server resolves the account's source itself
    (the user never picks "local vs directory"). A local account is tried first;
    if it does not authenticate and directory auth is enabled, the directory is
    tried. On success the session is established identically for both sources.

    On any failure the page re-renders with a single generic error so neither the
    username's existence, the resolved source, nor the failure reason is
    disclosed. A missing field is treated as the same generic failure. Repeated
    failures from one source are throttled with the same generic outcome; a failed
    attempt counts once regardless of how many sources were tried.
    """
    settings = _settings()
    username = form.get("username", "")
    password = form.get("password", "")
    remember_me = form.get("remember_me")
    next_target = _safe_next(form.get("next"))
    if not username or not password:
        return _login_error(request, next_target=next_target)

    throttle = _login_throttle()
    ip = _client_ip(request)

    if throttle.is_throttled(ip=ip, username=username):
        return _login_error(request, next_target=next_target)

    user = _resolve_login(db, username, password, settings=settings)
    if user is None:
        # One failure recorded for the attempt regardless of how many sources
        # were tried (a no-mapped-group directory denial and a directory outage
        # both land here, so both count toward the throttle).
        throttle.record_failure(ip=ip, username=username)
        return _login_error(request, next_target=next_target)

    throttle.record_success(ip=ip, username=username)
    old_token = request.cookies.get(settings.session.cookie_name) or ""
    persistent = bool(remember_me)
    row, raw_token = rotate_session(
        db, old_token, user, remember_me=persistent, settings=settings.session
    )
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
        response, request, raw_token, settings=settings, persistent=persistent
    )
    return response


def _resolve_login(
    db: DbSession, username: str, password: str, *, settings: Settings
) -> User | None:
    """Resolve a login to a :class:`User`, trying local then directory.

    A local account always authenticates locally (``authenticate_user`` filters to
    ``auth_source == "local"``). If no local account matches and directory auth is
    enabled, the directory is consulted; a directory user is provisioned/updated
    just-in-time and assigned the role mapped from its group membership.

    Returns ``None`` for every failure (wrong password, unknown user, no mapped
    group, directory unreachable/misconfigured, or directory disabled) so the
    caller presents one generic outcome. Directory infrastructure faults are
    logged with an admin-facing diagnostic; credentials are never logged.
    """
    user = authenticate_user(db, username, password, settings=settings.auth)
    if user is not None:
        return user

    ldap_view = load_ldap_settings(db)
    if not ldap_view.enabled:
        # Directory auth off (or never configured): behave exactly as local-only.
        return None

    result = ldap_authenticate(
        settings=ldap_view,
        username=username,
        password=password,
        bind_password=resolve_bind_password(db),
    )

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
            return None
        try:
            return provision_user(
                db,
                username=username,
                role=role,
                display_name=result.display_name,
            )
        except LdapProvisioningError:
            # A local account already owns this username; a directory login must
            # never take it over. Generic denial, with an admin-facing note.
            logger.warning("LDAP login refused: username collides with a local account")
            return None

    if result.outcome in (LdapOutcome.SERVER_UNREACHABLE, LdapOutcome.CONFIG_ERROR):
        # Surface a clear admin diagnostic; the user sees only a generic failure.
        logger.warning("LDAP login could not be completed: %s", result.detail)

    # INVALID_CREDENTIALS / NO_SUCH_USER / DISABLED / the faults above: generic
    # denial with no enumeration of which source or why.
    return None


def _login_error(request: Request, *, next_target: str = "/") -> Response:
    """Render the login page with the generic invalid-credentials message."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "csrf_token": "",
            "flash_messages": [],
            "error": True,
            "next": next_target,
            "app_version": get_app_version(),
        },
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.post("/logout")
def logout(request: Request, db: DbDep, user: CurrentUser) -> Response:
    """Revoke the current session and clear the cookie."""
    settings = _settings()
    raw_token = request.cookies.get(settings.session.cookie_name) or ""
    revoke_session(db, raw_token)
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
    """Render the first-run setup page, or redirect once setup is complete."""
    if not first_run_needed(db):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "first_run.html",
        {
            "request": request,
            "csrf_token": "",
            "errors": [],
            "form_values": {},
            "app_version": get_app_version(),
        },
    )


@router.post("/first-run")
def first_run_submit(request: Request, db: DbDep, form: FormDep) -> Response:
    """Create the initial administrator and sign them in.

    Idempotency guard: if an admin already exists this is rejected, so the route
    cannot be replayed to mint extra admins. Validates the password length and
    confirmation; on error the page re-renders with the messages. On success the
    new admin is created, a session is minted, and the cookie is set.
    """
    settings = _settings()
    if not first_run_needed(db):
        return RedirectResponse(url="/login", status_code=303)

    username = form.get("username", "")
    password = form.get("password", "")
    password_confirm = form.get("password_confirm", "")
    errors = _validate_new_account(
        username, password, password_confirm, settings=settings
    )
    if errors:
        return templates.TemplateResponse(
            request,
            "first_run.html",
            {
                "request": request,
                "csrf_token": "",
                "errors": errors,
                "form_values": {"username": username},
                "app_version": get_app_version(),
            },
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
    response = RedirectResponse(url="/", status_code=303)
    deps.set_session_cookie(
        response, request, raw_token, settings=settings, persistent=False
    )
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

"""User-administration routes: list, add, edit, password reset, session
revocation, enable/disable, and deletion.

These handlers are thin adapters over :mod:`timelapse_manager.services.users`:
they parse their form input, call the service (which validates, enforces the
account-security invariants, mutates, and flushes), and render the result. A
service-raised :class:`~timelapse_manager.services.errors.AppError` is caught and
turned back into the exact response the endpoint returned before the service
existed -- a bare ``404``/``400`` for a missing user or a rejected create, or an
inline form/row fragment re-render at ``200`` (rolled back first so the normal
return does not commit a partial mutation). The request-scoped session commits on
a clean return; user writes have no post-commit effect to run.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from ...db.models import User
from ...security.sessions import invalidate_request_session
from ...services import users as user_service
from ...services._base import Actor
from ...services.errors import AppError, ConflictError, NotFoundError
from ...services.users import UserSpec
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
    DbDep,
    FormDep,
    templates,
)
from ._shared import _settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _forget_own_session(request: Request, actor: User, target_id: int) -> None:
    """Drop the request's cached session when the admin acted on their own account.

    Revoking, disabling, demoting or deleting yourself changes the session this
    request authenticated with. The request's cached resolution (#67 R3) would
    otherwise still call it live for the rest of the request, for example when the
    row fragment renders its CSRF token (#208).
    """
    if target_id == actor.id:
        invalidate_request_session(request)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: DbDep, user: AdminUser) -> Response:
    """Render the user-accounts table.

    Shows all real accounts: local users with a password hash and directory
    (LDAP) users who never carry one.  Excludes the password-less sentinel
    admin (``auth_source="local"`` + ``password_hash IS NULL``) which exists
    only as an audit-event actor, not a real sign-in account.
    """
    users = (
        db.execute(
            select(User)
            .where(
                or_(
                    User.password_hash.is_not(None),
                    User.auth_source == "ldap",
                )
            )
            .order_by(User.id)
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "users.html",
        deps.base_context(request, db, user, users=users),
    )


@router.post("/users")
def create_user(
    request: Request, db: DbDep, user: AdminUser, form: FormDep
) -> Response:
    """Create a local Admin, Operator, or Viewer account."""
    spec = UserSpec(
        username=form.get("username", ""),
        password=form.get("password", ""),
        role=form.get("role", "viewer"),
        password_confirm=form.get("password_confirm"),
    )
    try:
        user_service.create_user(db, Actor.of(user), spec, settings=_settings())
    except ConflictError as exc:
        # A duplicate username surfaces as a 409 (the service turns the UNIQUE-race
        # IntegrityError into a ConflictError). Roll back the partial insert so the
        # request's session does not commit it.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=exc.detail
        ) from exc
    except AppError as exc:
        # Every other create failure (missing field, bad role, mismatch, weak
        # password) has always surfaced as a bare 400 -- the create form is a
        # normal, redirecting submit, not an HTMX swap -- so preserve that.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail
        ) from exc
    return RedirectResponse(url="/users", status_code=303)


@router.get("/users/add-form", response_class=HTMLResponse)
def user_add_form(request: Request, db: DbDep, user: AdminUser) -> Response:
    """Return the inline create-user form fragment for HTMX.

    Admin-only. The fragment posts to ``POST /users`` (a normal form submit that
    redirects), matching the existing create handler. Admin, Operator, and Viewer
    roles are offered.
    """
    return templates.TemplateResponse(
        request,
        "_partials/user_form.html",
        deps.base_context(request, db, user, account=None),
    )


@router.get("/users/new", response_class=HTMLResponse)
def new_user_page(request: Request, db: DbDep, user: AdminUser) -> Response:
    """Full-page create-user form (no-JS / direct-URL fallback for the drawer)."""
    return templates.TemplateResponse(
        request, "users_new.html", deps.base_context(request, db, user)
    )


@router.get("/drawers/new-user", response_class=HTMLResponse)
def new_user_drawer(request: Request, db: DbDep, user: AdminUser) -> Response:
    """Serve the create-user form as a drawer fragment, or the full page.

    Admin-only (enforced by the AdminUser dependency). An HTMX request gets the
    bare form fragment for the drawer body; a direct request gets the page.
    """
    template = (
        "_partials/drawer_new_user.html"
        if request.headers.get("HX-Request")
        else "users_new.html"
    )
    return templates.TemplateResponse(
        request, template, deps.base_context(request, db, user)
    )


@router.get("/users/{user_id:int}/edit-form", response_class=HTMLResponse)
def user_edit_form(
    request: Request, db: DbDep, user: AdminUser, user_id: int
) -> Response:
    """Return the inline edit-user form fragment, prefilled, for HTMX.

    The username is shown read-only and the current role is preselected. The
    fragment ``hx-post``s to the edit-apply route and swaps the user's row on
    success. Admin, Operator, and Viewer are offered as target roles.
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return templates.TemplateResponse(
        request,
        "_partials/user_form.html",
        deps.base_context(request, db, user, account=target),
    )


def _user_form_error(
    request: Request,
    db: DbSession,
    user: User,
    account: User | None,
    message: str,
) -> Response:
    """Re-render the user form fragment with an inline error, at 200.

    Returned at 200 so HTMX swaps the fragment and surfaces the message (it does
    not swap 4xx responses). The form keeps its mode (create when ``account`` is
    ``None``, edit otherwise) so the admin can correct and resubmit.
    """
    return templates.TemplateResponse(
        request,
        "_partials/user_form.html",
        deps.base_context(
            request,
            db,
            user,
            account=account,
            flash_messages=[{"type": "error", "message": message}],
        ),
    )


@router.post("/users/{user_id:int}/edit", response_class=HTMLResponse)
def edit_user(
    request: Request, db: DbDep, user: AdminUser, user_id: int, form: FormDep
) -> Response:
    """Apply an admin role change to a user and return its refreshed row.

    Role changes accept Admin, Operator, and Viewer. Two lockout guards protect
    the admin surface: demoting the last remaining real administrator (to either
    operator or viewer) is refused, and -- as a subset of that rule -- an admin
    cannot demote themselves into lockout. The username is immutable here; the
    edit form renders it read-only.
    """
    role = form.get("role", "")
    try:
        result = user_service.edit_user_role(db, Actor.of(user), user_id, role=role)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc
    except AppError as exc:
        # Invalid role / last-admin refusal: roll back and re-render the edit form
        # with an inline error at 200 (HTMX skips 4xx), exactly as before.
        db.rollback()
        target = db.get(User, user_id)
        return _user_form_error(request, db, user, target, exc.detail)
    _forget_own_session(request, user, user_id)
    return _user_row_response(request, db, user, result.value)


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
def reset_user_password(
    request: Request, db: DbDep, user: AdminUser, user_id: int, form: FormDep
) -> Response:
    """Reset a local user's password (revoking their sessions) and re-render row.

    When a new ``password`` is supplied it must be confirmed and satisfy the
    configured minimum length, then it is hashed and set; in either case the
    target's sessions are revoked so any stolen token dies. A directory (LDAP)
    account is refused -- its credential is managed externally and is not stored
    here, so it is never mutated. The row fragment is returned for the HTMX swap.
    """
    try:
        result = user_service.reset_user_password(
            db,
            Actor.of(user),
            user_id,
            password=form.get("password"),
            password_confirm=form.get("password_confirm"),
            settings=_settings(),
        )
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc
    except AppError as exc:
        # Directory account / confirmation / length problem: roll back and surface
        # the message on the row at 200 so HTMX still swaps it.
        db.rollback()
        target = db.get(User, user_id)
        assert target is not None  # past the service's not-found check
        return _user_row_response(request, db, user, target, error=exc.detail)
    _forget_own_session(request, user, user_id)
    return _user_row_response(request, db, user, result.value)


@router.post("/users/{user_id}/revoke-sessions", response_class=HTMLResponse)
def revoke_user_sessions(
    request: Request, db: DbDep, user: AdminUser, user_id: int
) -> Response:
    """Revoke all of a user's sessions and re-render their row."""
    try:
        result = user_service.revoke_user_sessions(db, Actor.of(user), user_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc
    _forget_own_session(request, user, user_id)
    return _user_row_response(request, db, user, result.value)


@router.post("/users/{user_id:int}/disable", response_class=HTMLResponse)
def disable_user(
    request: Request, db: DbDep, user: AdminUser, user_id: int
) -> Response:
    """Disable an account (it can no longer sign in) and re-render its row.

    Refuses to disable the last enabled administrator so the system is never left
    with no one who can sign in. Disabling revokes the target's live sessions so
    the lock-out takes effect immediately, not just at next login.
    """
    try:
        result = user_service.disable_user(db, Actor.of(user), user_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc
    except AppError as exc:
        db.rollback()
        target = db.get(User, user_id)
        assert target is not None  # past the service's not-found check
        return _user_row_response(request, db, user, target, error=exc.detail)
    _forget_own_session(request, user, user_id)
    return _user_row_response(request, db, user, result.value)


@router.post("/users/{user_id:int}/enable", response_class=HTMLResponse)
def enable_user(request: Request, db: DbDep, user: AdminUser, user_id: int) -> Response:
    """Re-enable a disabled account and re-render its row."""
    try:
        result = user_service.enable_user(db, Actor.of(user), user_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc
    return _user_row_response(request, db, user, result.value)


@router.post("/users/{user_id:int}/delete")
def delete_user(request: Request, db: DbDep, user: AdminUser, user_id: int) -> Response:
    """Remove a user account entirely, then return to the users page.

    Refuses to remove the last enabled administrator. The target's sessions are
    revoked before the row is deleted so no live token outlives the account.
    """
    try:
        user_service.delete_user(db, Actor.of(user), user_id)
        _forget_own_session(request, user, user_id)
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not found"
        ) from exc
    except AppError as exc:
        # Last-admin refusal: re-render the table with an inline error at 400
        # rather than deleting, exactly as before.
        db.rollback()
        users = (
            db.execute(
                select(User)
                .where(or_(User.password_hash.is_not(None), User.auth_source == "ldap"))
                .order_by(User.id)
            )
            .scalars()
            .all()
        )
        return templates.TemplateResponse(
            request,
            "users.html",
            deps.base_context(
                request,
                db,
                user,
                users=users,
                flash_messages=[{"type": "error", "message": exc.detail}],
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse(url="/users", status_code=303)


def _user_row_response(
    request: Request,
    db: DbSession,
    current: User,
    row_user: User,
    *,
    error: str | None = None,
) -> Response:
    """Render the single user-row fragment after a per-user mutation.

    The template expects the row's account under ``user`` and the acting admin
    under ``current_user``; the base context already supplies the latter, so the
    ``user`` key is set to the row account explicitly. An optional ``error`` is
    surfaced as a flash message (returned at 200 so HTMX still swaps the row).
    """
    context = deps.base_context(request, db, current)
    context["user"] = row_user
    if error is not None:
        context["flash_messages"] = [{"type": "error", "message": error}]
    return templates.TemplateResponse(request, "_partials/user_row.html", context)

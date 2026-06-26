"""User-administration routes: list, add, edit, password reset, session
revocation, enable/disable, and deletion."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from ...db.models import User
from ...security import (
    hash_password,
    revoke_all_user_sessions,
)
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
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
    settings = _settings()
    username = form.get("username", "")
    password = form.get("password", "")
    role = form.get("role", "viewer")
    password_confirm = form.get("password_confirm")
    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="username and password are required",
        )
    if role not in (deps.ADMIN_ROLE, deps.OPERATOR_ROLE, deps.VIEWER_ROLE):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid role"
        )
    if password_confirm is not None and password != password_confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="passwords do not match"
        )
    if len(password) < settings.auth.password_min_length:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="password too short"
        )
    new_user = User(
        username=username,
        auth_source="local",
        password_hash=hash_password(password, settings.auth),
        role=role,
        enabled=True,
    )
    db.add(new_user)
    db.flush()
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message=f"user {new_user.username!r} ({role}) created",
    )
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


def _real_admin_count(db: DbSession) -> int:
    """Count real administrator accounts (excluding the password-less sentinel).

    The seeded sentinel admin has a NULL password hash and cannot sign in, so it
    is not a real admin for lockout purposes -- mirroring the users-page filter.
    """
    return db.execute(
        select(func.count())
        .select_from(User)
        .where(User.role == deps.ADMIN_ROLE)
        .where(User.password_hash.is_not(None))
    ).scalar_one()


def _enabled_admin_count(db: DbSession) -> int:
    """Count real administrators that can actually sign in (enabled, with a hash).

    This is the lockout basis for *disable* and *delete*: an account that is
    disabled or sentinel cannot administer the system, so only enabled real admins
    keep the system reachable.
    """
    return db.execute(
        select(func.count())
        .select_from(User)
        .where(User.role == deps.ADMIN_ROLE)
        .where(User.password_hash.is_not(None))
        .where(User.enabled.is_(True))
    ).scalar_one()


def _is_last_enabled_admin(db: DbSession, target: User) -> bool:
    """Return True if ``target`` is the only enabled real administrator left.

    Used to refuse a disable/delete that would leave the system with no
    administrator who can sign in. A non-admin, sentinel, or already-disabled
    target is never the last enabled admin.
    """
    if (
        target.role != deps.ADMIN_ROLE
        or target.password_hash is None
        or not target.enabled
    ):
        return False
    return _enabled_admin_count(db) <= 1


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
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    role = form.get("role", "")
    if role not in (deps.ADMIN_ROLE, deps.OPERATOR_ROLE, deps.VIEWER_ROLE):
        return _user_form_error(request, db, user, target, "Invalid role.")

    # Any change *away* from admin is a demotion for lockout purposes -- whether
    # to operator or viewer, the account stops counting as a real administrator.
    demoting_admin = target.role == deps.ADMIN_ROLE and role != deps.ADMIN_ROLE
    if demoting_admin and _real_admin_count(db) <= 1:
        # This is the last real admin (and, when self-editing, also the
        # self-demotion-into-lockout case): refuse so the system keeps an admin.
        return _user_form_error(
            request,
            db,
            user,
            target,
            "Cannot demote the last remaining administrator.",
        )

    if role != target.role:
        target.role = role
        db.flush()
        _audit(
            db,
            scope="system",
            scope_id=None,
            actor_user_id=user.id,
            message=f"role for user {target.username!r} changed to {role}",
        )
    return _user_row_response(request, db, user, target)


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
    settings = _settings()
    password = form.get("password")
    password_confirm = form.get("password_confirm")
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    # A directory account carries no local password; never mutate it. Returned at
    # 200 so HTMX swaps the row and surfaces the inline error (it skips 4xx).
    if target.auth_source != "local":
        return _user_row_response(
            request,
            db,
            user,
            target,
            error=(
                "This account is managed by the directory; its password "
                "cannot be set here."
            ),
        )
    if password:
        if password_confirm is not None and password != password_confirm:
            return _user_row_response(
                request, db, user, target, error="Passwords do not match."
            )
        if len(password) < settings.auth.password_min_length:
            return _user_row_response(
                request,
                db,
                user,
                target,
                error=(
                    "Password must be at least "
                    f"{settings.auth.password_min_length} characters."
                ),
            )
        target.password_hash = hash_password(password, settings.auth)
        db.flush()
        password_set = True
    else:
        password_set = False
    revoke_all_user_sessions(db, target.id)
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message=(
            f"password set for user {target.username!r} by {user.username!r}"
            if password_set
            else f"sessions revoked for user {target.username!r} by {user.username!r}"
        ),
    )
    return _user_row_response(request, db, user, target)


@router.post("/users/{user_id}/revoke-sessions", response_class=HTMLResponse)
def revoke_user_sessions(
    request: Request, db: DbDep, user: AdminUser, user_id: int
) -> Response:
    """Revoke all of a user's sessions and re-render their row."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    revoke_all_user_sessions(db, target.id)
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message=f"sessions revoked for user {target.username!r}",
    )
    return _user_row_response(request, db, user, target)


@router.post("/users/{user_id:int}/disable", response_class=HTMLResponse)
def disable_user(
    request: Request, db: DbDep, user: AdminUser, user_id: int
) -> Response:
    """Disable an account (it can no longer sign in) and re-render its row.

    Refuses to disable the last enabled administrator so the system is never left
    with no one who can sign in. Disabling revokes the target's live sessions so
    the lock-out takes effect immediately, not just at next login.
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if _is_last_enabled_admin(db, target):
        return _user_row_response(
            request,
            db,
            user,
            target,
            error="Cannot disable the last remaining administrator.",
        )
    if target.enabled:
        target.enabled = False
        db.flush()
        revoke_all_user_sessions(db, target.id)
        _audit(
            db,
            scope="system",
            scope_id=None,
            actor_user_id=user.id,
            message=f"user {target.username!r} disabled",
        )
    return _user_row_response(request, db, user, target)


@router.post("/users/{user_id:int}/enable", response_class=HTMLResponse)
def enable_user(request: Request, db: DbDep, user: AdminUser, user_id: int) -> Response:
    """Re-enable a disabled account and re-render its row."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not target.enabled:
        target.enabled = True
        db.flush()
        _audit(
            db,
            scope="system",
            scope_id=None,
            actor_user_id=user.id,
            message=f"user {target.username!r} enabled",
        )
    return _user_row_response(request, db, user, target)


@router.post("/users/{user_id:int}/delete")
def delete_user(request: Request, db: DbDep, user: AdminUser, user_id: int) -> Response:
    """Remove a user account entirely, then return to the users page.

    Refuses to remove the last enabled administrator. The target's sessions are
    revoked before the row is deleted so no live token outlives the account.
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if _is_last_enabled_admin(db, target):
        # Re-render the table with an inline error rather than deleting.
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
                flash_messages=[
                    {
                        "type": "error",
                        "message": "Cannot remove the last remaining administrator.",
                    }
                ],
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    username = target.username
    revoke_all_user_sessions(db, target.id)
    db.delete(target)
    db.flush()
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message=f"user {username!r} removed",
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

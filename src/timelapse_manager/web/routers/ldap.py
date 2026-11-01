"""LDAP/directory settings routes, including the connection test and camera-
credential defaults."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)

from ...runtime import get_context
from ...security.camera_defaults_service import (
    CameraDefaultsUpdate,
)
from ...security.camera_defaults_service import (
    update_settings as update_camera_defaults,
)
from ...security.ldap_directory import (
    LdapOutcome,
)
from ...security.ldap_directory import (
    resolve_directory_state as ldap_resolve_directory_state,
)
from ...security.ldap_settings_service import (
    LdapSettingsError,
    LdapSettingsUpdate,
    resolve_bind_password,
)
from ...security.ldap_settings_service import load_settings as load_ldap_settings
from ...security.ldap_settings_service import update_settings as update_ldap_settings
from ...security.session_revalidation import breaker_for
from ...services._base import Effect, run_effects
from .. import dependencies as deps
from ..dependencies import (
    AdminUser,
    DbDep,
    FormDep,
    templates,
)
from ._shared import (
    _LDAP_CONNECTION_MESSAGES,
    _audit,
    _ldap_context,
    _parse_lines,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/settings/ldap")
def ldap_settings_submit(
    request: Request, db: DbDep, user: AdminUser, form: FormDep
) -> Response:
    """Persist the LDAP directory settings.

    The bind password follows the masked write-back rule delegated entirely to
    the settings service: a blank, ``None``, or ``***`` submission preserves
    the stored encrypted secret without re-encrypting it.

    Validation gate: when ``enabled`` is checked the form must supply at least
    one server URL, a user search base, and a username attribute, or the save is
    refused and the partial is re-rendered with an inline error.  A disabled
    configuration may be saved incomplete (no live logins use it).  An unknown
    TLS mode is refused the same way.

    Retargeting the directory (servers, TLS mode or bind DN) drops the stored
    bind password unless a new one is typed (see the settings service). After the
    save is committed the directory circuit breaker is reset (#203): what it
    remembered was about the previous configuration.
    """
    enabled = bool(form.get("ldap_enabled"))
    server_urls = _parse_lines(form.get("ldap_server_urls"))
    search_base = (form.get("ldap_search_base") or "").strip()
    username_attribute = (form.get("ldap_username_attribute") or "").strip()

    if enabled:
        missing: list[str] = []
        if not server_urls:
            missing.append("at least one server URL")
        if not search_base:
            missing.append("user search base")
        if not username_attribute:
            missing.append("username attribute")
        if missing:
            ctx = _ldap_context(
                request,
                db,
                user,
                error="Cannot enable LDAP without: " + ", ".join(missing) + ".",
            )
            return templates.TemplateResponse(
                request,
                "settings.html",
                ctx,
            )

    update = LdapSettingsUpdate(
        enabled=enabled,
        server_urls=server_urls,
        tls_mode=form.get("ldap_tls_mode", "ldaps"),
        tls_ca_cert_path=(form.get("ldap_tls_ca_cert_path") or "").strip() or None,
        bind_dn=form.get("ldap_bind_dn") or None,
        bind_password=form.get("ldap_bind_password"),
        search_base=search_base or None,
        search_filter=form.get("ldap_search_filter") or None,
        group_search_base=form.get("ldap_group_search_base") or None,
        username_attribute=username_attribute or None,
        display_name_attribute=form.get("ldap_display_name_attribute") or None,
        membership_mode=form.get("ldap_membership_mode", "memberof"),
        nested_groups=bool(form.get("ldap_nested_groups")),
        admin_group_dn=form.get("ldap_admin_group_dn") or None,
        admin_group_filter=form.get("ldap_admin_group_filter") or None,
        operator_group_dn=form.get("ldap_operator_group_dn") or None,
        operator_group_filter=form.get("ldap_operator_group_filter") or None,
        viewer_group_dn=form.get("ldap_viewer_group_dn") or None,
        viewer_group_filter=form.get("ldap_viewer_group_filter") or None,
    )
    try:
        update_ldap_settings(db, update)
    except LdapSettingsError as exc:
        ctx = _ldap_context(request, db, user, error=str(exc))
        return templates.TemplateResponse(request, "settings.html", ctx)
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message="LDAP settings updated",
    )
    # Commit first, then reset: a probe racing the reset must read the new
    # settings, or its failure against the old ones would re-open the breaker.
    db.commit()
    breaker_for(db).reset()
    return RedirectResponse(url="/settings#ldap", status_code=303)


@router.post("/settings/camera-credentials")
def camera_defaults_submit(
    request: Request, db: DbDep, user: AdminUser, form: FormDep
) -> Response:
    """Persist the global default camera credentials.

    The password follows the masked write-back rule delegated entirely to the
    settings service: a blank, ``None``, or ``***`` submission preserves the
    stored encrypted secret without re-encrypting it. The password is never
    echoed back to the page (only the mask sentinel is rendered). After the
    commit the capture supervisor is woken to reload cameras that inherit the
    defaults (#152).
    """
    update = CameraDefaultsUpdate(
        enabled=bool(form.get("camera_defaults_enabled")),
        username=(form.get("camera_defaults_username") or "").strip() or None,
        password=form.get("camera_defaults_password"),
    )
    update_camera_defaults(db, update)
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message="default camera credentials updated",
    )
    db.commit()
    # Cameras that inherit the defaults have them in their cached capture
    # session; the reconcile pass sees the changed fingerprint and reloads them
    # now rather than up to one reconcile interval later (#152).
    run_effects((Effect.CAPTURE_RECONCILE,), get_context())
    return RedirectResponse(url="/settings#camera-credentials", status_code=303)


@router.post("/settings/ldap/test-connection", response_class=HTMLResponse)
def ldap_test_connection(request: Request, db: DbDep, user: AdminUser) -> Response:
    """Run a service-bind reachability check against the saved LDAP configuration.

    Uses the stored (saved) settings so the bind password is read from the
    database via :func:`resolve_bind_password` — the form's masked value is
    never submitted here.  The test performs only a service bind and a directory
    lookup probe; no user credentials are checked and the bind password is
    never echoed back. Because a retarget save clears the stored password unless a
    new one is typed, the saved password always belongs to the saved target.

    A successful test resets the directory circuit breaker.

    Returns an HTML fragment (the result banner) for HTMX injection.
    """
    view = load_ldap_settings(db)
    bind_pw = resolve_bind_password(db)

    # Use a placeholder username that will not match any real entry; the
    # connection test only needs to prove the service bind succeeds.
    result = ldap_resolve_directory_state(
        settings=view,
        username="__connection_probe__",
        bind_password=bind_pw,
    )

    message = _LDAP_CONNECTION_MESSAGES.get(result.outcome, "Unknown result")
    ok = result.outcome in (LdapOutcome.AUTHENTICATED, LdapOutcome.NO_SUCH_USER)
    if ok:
        # The directory just answered with the saved settings: an open breaker
        # (#203) is stale. Deliberately not gated on the breaker: the test is
        # most needed while it is open.
        breaker_for(db).reset()
    status_class = "success" if ok else "error"

    return templates.TemplateResponse(
        request,
        "_partials/ldap_test_result.html",
        deps.base_context(
            request,
            db,
            user,
            ldap_test_message=message,
            ldap_test_ok=ok,
            ldap_test_status_class=status_class,
        ),
    )

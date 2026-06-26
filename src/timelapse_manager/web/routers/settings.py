"""Admin settings page routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)

from ...security.ssrf_settings_service import apply_to_runtime as apply_ssrf_runtime
from ...security.ssrf_settings_service import normalise_subnets
from ...security.ssrf_settings_service import update_settings as update_ssrf_settings
from ..dependencies import (
    AdminUser,
    DbDep,
    FormDep,
    templates,
)
from ._shared import (
    _audit,
    _ldap_context,
    _parse_lines,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DbDep, user: AdminUser) -> Response:
    """Render the admin settings page from the live configuration."""
    return templates.TemplateResponse(
        request, "settings.html", _ldap_context(request, db, user)
    )


@router.post("/settings")
def settings_submit(request: Request, db: DbDep, user: AdminUser) -> Response:
    """Accept a settings edit (audited) and redirect back.

    Settings are resolved from config/env at startup and are not hot-reloaded
    here; the form submission is recorded as an audited intent and the page
    re-rendered. Applying changes that require a restart is out of this layer's
    scope.
    """
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message="settings update submitted",
    )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/ssrf")
def ssrf_settings_submit(
    request: Request, db: DbDep, user: AdminUser, form: FormDep
) -> Response:
    """Persist the admin-managed SSRF private-subnet allow-list and apply it live.

    The textarea is one CIDR (or bare IP) per line. Entries are validated and
    canonicalised with the same parser the guard uses; a bare host becomes a
    ``/32``. If any entry is unparsable the save is refused and the form is
    re-rendered with an inline error -- a silently-dropped bad entry would leave
    the operator thinking a subnet was authorised when it was not.

    On success the stored list is merged with the config/env baseline onto the
    running policy via :func:`apply_ssrf_runtime`, so a newly authorised subnet
    takes effect immediately -- no restart -- and the env-provided baseline is
    preserved. Editing the allow-list is a security-relevant action, so it is
    audited.
    """
    raw = _parse_lines(form.get("ssrf_allowed_private_subnets"))
    normalised, invalid = normalise_subnets(raw)
    if invalid:
        ctx = _ldap_context(
            request,
            db,
            user,
            ssrf_error=("Not a valid CIDR or IP address: " + ", ".join(invalid) + "."),
        )
        return templates.TemplateResponse(request, "settings.html", ctx)

    update_ssrf_settings(db, normalised)
    apply_ssrf_runtime(db)
    _audit(
        db,
        scope="system",
        scope_id=None,
        actor_user_id=user.id,
        message="SSRF allowed private subnets updated",
    )
    return RedirectResponse(url="/settings#network", status_code=303)

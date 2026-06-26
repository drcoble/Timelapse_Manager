"""Read/update the singleton ``ssrf_settings`` row and apply it to the runtime.

This is the CRUD seam over the single ``ssrf_settings`` row (its primary key is
constrained to ``1``) plus the small bit of wiring that makes an admin's edits
take effect without a restart.

The effective SSRF allow-list the camera/scan guard uses is the **union** of two
sources:

* the *config baseline* -- subnets supplied by the config file or the
  ``TLM_SSRF__ALLOWED_PRIVATE_SUBNETS`` environment variable, captured once at
  startup into :attr:`AppContext.ssrf_config_subnets`; and
* the *admin list* -- the subnets stored in this table, edited from the web UI.

:func:`apply_to_runtime` recomputes that union and rebinds it onto the running
settings, so a subnet added in the UI is honoured immediately while the
environment-provided baseline is never lost. The stored list only ever widens
the camera/scan surface; the always-blocked ranges (loopback/link-local/
cloud-metadata) and the outbound-webhook surface are unaffected.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..db.models import SsrfSettings
from ..runtime import get_context

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The singleton row's fixed primary key.
_ROW_ID = 1


@dataclass(frozen=True)
class SsrfSettingsView:
    """A display projection of the SSRF settings row.

    Holds only the admin-managed subnets (the config-baseline subnets are shown
    separately, as read-only, by the settings page). No secrets are involved.
    """

    allowed_private_subnets: list[str] = field(default_factory=list)


def normalise_subnets(entries: Iterable[str]) -> tuple[list[str], list[str]]:
    """Validate and canonicalise admin-entered CIDR/IP entries.

    Returns ``(normalised, invalid)``. Each accepted entry is parsed with
    :func:`ipaddress.ip_network` in non-strict mode -- so a bare host such as
    ``10.1.16.30`` becomes ``10.1.16.30/32`` and ``10.1.16.30/24`` is canonicalised
    to its network ``10.1.16.0/24`` -- using the *same* parser the guard applies
    when matching, so the page never accepts an entry the guard would silently
    drop. Blank lines are ignored; duplicates collapse; order is preserved. Any
    entry that does not parse is returned verbatim in ``invalid`` so the caller can
    refuse the save with a precise message rather than persisting a no-op.
    """
    normalised: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in entries:
        text = raw.strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            invalid.append(text)
            continue
        canonical = str(network)
        if canonical not in seen:
            seen.add(canonical)
            normalised.append(canonical)
    return normalised, invalid


def load_settings(session: Session) -> SsrfSettingsView:
    """Return the stored admin subnet list as a display view.

    A missing singleton row yields an empty view, so the settings page renders
    cleanly on a fresh install with nothing configured yet.
    """
    row = session.get(SsrfSettings, _ROW_ID)
    if row is None:
        return SsrfSettingsView()
    return SsrfSettingsView(
        allowed_private_subnets=_as_str_list(row.allowed_private_subnets)
    )


def update_settings(session: Session, subnets: Sequence[str]) -> SsrfSettingsView:
    """Replace the stored admin subnet list (creating the row on first write).

    ``subnets`` is expected to already be normalised by :func:`normalise_subnets`.
    Returns the refreshed view; the caller's surrounding transaction commits.
    """
    row = session.get(SsrfSettings, _ROW_ID)
    if row is None:
        row = SsrfSettings(id=_ROW_ID)
        session.add(row)
    row.allowed_private_subnets = [s for s in subnets if s]
    session.flush()
    return load_settings(session)


def effective_subnets(
    config_subnets: Iterable[str], db_subnets: Iterable[str]
) -> list[str]:
    """Union the config-baseline and admin subnets, baseline first, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for subnet in (*config_subnets, *db_subnets):
        if subnet and subnet not in seen:
            seen.add(subnet)
            out.append(subnet)
    return out


def apply_to_runtime(session: Session) -> list[str]:
    """Merge the stored admin subnets onto the config baseline in the live runtime.

    Recomputes ``config baseline ∪ admin list`` and **rebinds** it onto
    ``settings.ssrf.allowed_private_subnets`` as a fresh list -- never an in-place
    mutation -- so a camera/scan guard call that has already captured the prior
    list reference keeps iterating it safely. After this returns, the camera and
    scan surfaces honour the merged list without a restart. Returns the merged
    list (handy for logging/tests).
    """
    context = get_context()
    db_subnets = load_settings(session).allowed_private_subnets
    merged = effective_subnets(context.ssrf_config_subnets, db_subnets)
    # Rebind (assignment), not `.clear()/.extend()`: the running guard may hold a
    # reference to the old list mid-check, so we never mutate it in place. The
    # field now carries config∪DB at runtime; the pure config baseline is kept on
    # the context as ``ssrf_config_subnets``.
    context.settings.ssrf.allowed_private_subnets = merged
    return merged


def _as_str_list(value: object) -> list[str]:
    """Coerce a stored JSON value into a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]

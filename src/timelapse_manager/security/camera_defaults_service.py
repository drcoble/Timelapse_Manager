"""Read and update the singleton default-camera-credentials row.

This is the CRUD seam over the single ``camera_default_credentials`` row (its
primary key is constrained to ``1``). It enforces the one correctness rule the
web form alone cannot: the fallback **password** is masked when read for display
and must not be overwritten with the mask when an unchanged form is saved.

Secret handling and encryption at rest
--------------------------------------
The password is the one secret this row carries. It is encrypted on write and
never returned in plaintext from the display path:

* :func:`load_settings` returns the mask sentinel ``"***"`` for display when a
  password is stored (and an empty string when none is).
* :func:`update_settings` treats an empty / unchanged / sentinel-valued submitted
  password as "leave the stored secret alone"; only a genuinely new value
  overwrites it, and only that new value is encrypted (the keep-stored branch
  returns the already-encrypted ciphertext untouched, so it is never
  double-wrapped).
* :func:`resolve_default_credentials` is the separate, explicit decrypt-at-use
  resolver the capture path calls. It returns the same ``(username, password)``
  tuple shape the per-camera credential reader returns, so a caller can treat a
  default and a per-camera login identically. The password is never logged.

Encryption uses the versioned ``enc:v1:`` ciphertext prefix, so a legacy
plaintext value (written before encryption existed) is recognised on read and
returned unchanged, then silently re-encrypted the next time the field is
written. No data migration is required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..db.models import CameraDefaultCredentials
from .crypto import decrypt_secret, encrypt_secret

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# The sentinel a stored password is masked to for display, and the value the
# update path recognises as "unchanged -- keep the stored secret".
MASK_SENTINEL = "***"

# The singleton row's fixed primary key.
_ROW_ID = 1


@dataclass(frozen=True)
class CameraDefaultsView:
    """A display-safe projection of the default-credentials row.

    The password is never the real secret here: it is :data:`MASK_SENTINEL` when
    one is stored and an empty string when none is.
    """

    enabled: bool = False
    username: str = ""
    password: str = ""  # masked: MASK_SENTINEL if set, "" if not
    password_set: bool = False


@dataclass(frozen=True)
class CameraDefaultsUpdate:
    """The fields an admin may submit to update the default credentials.

    ``password`` follows the masked write-back rule: ``None``, an empty string,
    or :data:`MASK_SENTINEL` all mean "keep the stored secret". Only a different,
    non-empty value replaces it.
    """

    enabled: bool
    username: str | None
    password: str | None


def load_settings(session: Session) -> CameraDefaultsView:
    """Return the current settings as a display-safe view (masking the password).

    A missing singleton row yields an all-default view, so the settings page
    renders cleanly on a fresh install with nothing configured yet.
    """
    row = session.get(CameraDefaultCredentials, _ROW_ID)
    if row is None:
        return CameraDefaultsView()
    has_password = bool(row.password)
    return CameraDefaultsView(
        enabled=bool(row.enabled),
        username=row.username or "",
        password=MASK_SENTINEL if has_password else "",
        password_set=has_password,
    )


def update_settings(
    session: Session, update: CameraDefaultsUpdate
) -> CameraDefaultsView:
    """Apply an update to the singleton row (creating it on first write).

    Implements the masked write-back rule for the password: the stored secret is
    preserved unless the submitted value is genuinely new. Returns the refreshed,
    masked view. The caller's surrounding transaction commits.
    """
    row = session.get(CameraDefaultCredentials, _ROW_ID)
    if row is None:
        row = CameraDefaultCredentials(id=_ROW_ID)
        session.add(row)

    row.enabled = bool(update.enabled)
    row.username = update.username or None
    row.password = _resolve_password(update.password, row.password)

    session.flush()
    return load_settings(session)


def resolve_default_credentials(session: Session) -> tuple[str, str] | None:
    """Return the decrypted ``(username, password)`` fallback, or ``None``.

    This is the decrypt-at-use seam the capture path resolves once at load time
    and passes down to the adapter factory. Returns ``None`` when the fallback is
    disabled, no row exists, or no username is configured (a fallback with no
    username cannot authenticate). A missing password is treated as an empty
    string, matching how the per-camera credential reader behaves. A legacy
    plaintext password passes through unchanged. The result is never logged.
    """
    row = session.get(CameraDefaultCredentials, _ROW_ID)
    if row is None or not row.enabled or not row.username:
        return None
    password = decrypt_secret(row.password) if row.password else ""
    return (str(row.username), password)


def _resolve_password(submitted: str | None, stored: str | None) -> str | None:
    """Return the password to persist, honouring the masked write-back rule.

    A blank submission or the mask sentinel means "the admin did not type a new
    password" -- keep whatever is stored (already-encrypted ciphertext) untouched.
    Any other value is a genuine change and is encrypted before storage. Only the
    new-value branch encrypts, so an unchanged secret is never double-wrapped.
    """
    if submitted is None or submitted == "" or submitted == MASK_SENTINEL:
        return stored
    return encrypt_secret(submitted)

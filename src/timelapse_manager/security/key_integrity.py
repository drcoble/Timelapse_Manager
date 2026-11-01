"""Startup check that the at-rest key is the key that encrypted the data (#67 S8).

The encryption key lives outside the database (a key file or the OS keystore)
while the ciphertext lives inside it, so the two can drift apart: a restored
backup, a wiped key file, a switch of key store, an interrupted rotation. Before
this check, a missing key file was silently replaced by a fresh key, after which
every stored credential failed to decrypt, one capture or one login at a time.

``secrets_meta`` records the provider and a fingerprint of the key. At startup
:func:`ensure_key_integrity` compares the key it can load with that record and
**refuses to start** (:class:`KeyIntegrityError`) when ciphertext exists and the
key is missing or different. It never mints a key while ciphertext exists.

Upgrade
-------
An install from before this check has no ``secrets_meta`` row. The current key
is then tested against one stored ciphertext per column; if it decrypts, the row
is recorded and startup proceeds, so an upgrade cannot lock anyone out. If it
does not decrypt, the install was already broken and is reported.

Refusal
-------
The service exits with :data:`EXIT_KEY_INTEGRITY` (78), which the systemd unit
lists in ``RestartPreventExitStatus``: the fault needs an operator, and a
restart loop cannot fix it. The message names the key location and the first
eight characters of the expected fingerprint, never key material.

This module also holds the one registry of encrypted columns, shared by the
boot check, ``secrets status``, ``secrets rotate`` and ``secrets reset`` so the
four cannot disagree about what is encrypted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from ..db.models import (
    Camera,
    CameraDefaultCredentials,
    LdapSettings,
    NotificationSettings,
    SecretsMeta,
)
from .crypto import _PREFIX, _is_secret_field, is_encrypted
from .keystore import (
    KeyProvider,
    build_key_provider,
    fingerprint_prefix,
    key_fingerprint,
    provider_for_name,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from ..config.settings import Settings

logger = logging.getLogger(__name__)

#: Exit status when startup is refused over the key. ``EX_CONFIG`` from
#: ``sysexits.h``; distinct from the generic startup failure (3) so the unit's
#: ``RestartPreventExitStatus`` can stop a restart loop only for this fault.
EXIT_KEY_INTEGRITY = 78

# Set when :func:`ensure_key_integrity` refuses, so the process-level runner
# (which only sees that the lifespan failed) can choose the exit status.
_refused = False


def key_integrity_refused() -> bool:
    """Whether this process refused to start over the encryption key."""
    return _refused


def reset_key_integrity_refusal() -> None:
    """Clear the refusal flag (tests; a fresh start in the same process)."""
    global _refused
    _refused = False


class KeyIntegrityError(RuntimeError):
    """The loaded key is not the key that encrypted the stored secrets.

    The message is written for the operator (it ends up in the journal) and
    contains no key material.
    """


@dataclass(frozen=True)
class SecretColumn:
    """One place the application stores ciphertext."""

    label: str
    model: type[Any]
    attribute: str
    #: ``True`` for a JSON document whose secret-named keys hold the ciphertext.
    document: bool = False


#: Every encrypted column. Adding a place that stores ``enc:v1:`` values means
#: adding it here, or rotation would leave it on the old key.
SECRET_COLUMNS: tuple[SecretColumn, ...] = (
    SecretColumn("camera credentials", Camera, "credentials", document=True),
    SecretColumn("default camera credentials", CameraDefaultCredentials, "password"),
    SecretColumn("LDAP bind password", LdapSettings, "bind_password"),
    SecretColumn("SMTP password", NotificationSettings, "smtp_password"),
    SecretColumn(
        "webhook signing secret", NotificationSettings, "webhook_signing_secret"
    ),
)


def _encrypted_values(value: object, column: SecretColumn) -> Iterator[str]:
    """The ciphertext strings inside one stored cell."""
    if column.document:
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    _is_secret_field(str(key))
                    and isinstance(item, str)
                    and is_encrypted(item)
                ):
                    yield item
        return
    if isinstance(value, str) and is_encrypted(value):
        yield value


def sample_ciphertexts(session: Session) -> list[str]:
    """One stored ciphertext per column (the first found), for a key test."""
    samples: list[str] = []
    for column in SECRET_COLUMNS:
        attribute = getattr(column.model, column.attribute)
        for value in session.execute(select(attribute)).scalars():
            first = next(_encrypted_values(value, column), None)
            if first is not None:
                samples.append(first)
                break
    return samples


def count_ciphertexts(session: Session) -> dict[str, int]:
    """Encrypted values per column label (columns with none are omitted)."""
    counts: dict[str, int] = {}
    for column in SECRET_COLUMNS:
        attribute = getattr(column.model, column.attribute)
        total = sum(
            sum(1 for _ in _encrypted_values(value, column))
            for value in session.execute(select(attribute)).scalars()
        )
        if total:
            counts[column.label] = counts.get(column.label, 0) + total
    return counts


def has_ciphertext(session: Session) -> bool:
    """Whether any encrypted value is stored. Legacy plaintext does not count."""
    return bool(sample_ciphertexts(session))


def decrypts_with(cipher: Fernet, ciphertext: str) -> bool:
    """Whether ``cipher`` decrypts one ``enc:v1:`` value."""
    try:
        cipher.decrypt(ciphertext[len(_PREFIX) :].encode("ascii"))
    except (InvalidToken, ValueError):
        return False
    return True


def reencrypt_all(session: Session, old: Fernet, new: Fernet) -> int:
    """Re-encrypt every stored ciphertext from ``old`` to ``new``; return the count.

    Raises :class:`~cryptography.fernet.InvalidToken` on a value ``old`` cannot
    decrypt, so a rotation never half-applies: the caller's transaction rolls
    back and the old key stays in place.
    """

    def swap(token: str) -> str:
        plain = old.decrypt(token[len(_PREFIX) :].encode("ascii"))
        return _PREFIX + new.encrypt(plain).decode("ascii")

    total = 0
    for column in SECRET_COLUMNS:
        for row in session.execute(select(column.model)).scalars():
            current = getattr(row, column.attribute)
            if column.document:
                if not isinstance(current, dict):
                    continue
                updated = dict(current)
                for key, item in current.items():
                    if (
                        _is_secret_field(str(key))
                        and isinstance(item, str)
                        and is_encrypted(item)
                    ):
                        updated[key] = swap(item)
                        total += 1
                # A new dict, so the JSON column registers as changed.
                setattr(row, column.attribute, updated)
            elif isinstance(current, str) and is_encrypted(current):
                setattr(row, column.attribute, swap(current))
                total += 1
    session.flush()
    return total


def clear_all_ciphertext(session: Session) -> int:
    """Remove every stored ciphertext; return the count. Plaintext is untouched.

    For ``secrets reset``: ciphertext the current key cannot read is unusable, so
    it is dropped (a credential document keeps its non-secret fields, such as
    the username). Legacy plaintext values are still readable and stay.
    """
    total = 0
    for column in SECRET_COLUMNS:
        for row in session.execute(select(column.model)).scalars():
            current = getattr(row, column.attribute)
            if column.document:
                if not isinstance(current, dict):
                    continue
                kept = {
                    key: item
                    for key, item in current.items()
                    if not (
                        _is_secret_field(str(key))
                        and isinstance(item, str)
                        and is_encrypted(item)
                    )
                }
                removed = len(current) - len(kept)
                if removed:
                    setattr(row, column.attribute, kept)
                    total += removed
            elif isinstance(current, str) and is_encrypted(current):
                setattr(row, column.attribute, None)
                total += 1
    session.flush()
    return total


def recover_staged_key(provider: KeyProvider, recorded_fingerprint: str) -> None:
    """Finish or discard a rotation that did not complete.

    A staged key whose fingerprint matches the committed row means the database
    was re-encrypted but the promotion did not happen: promote it. A staged key
    that does not match belongs to a rotation that failed before its commit: the
    stored key is still the right one, so the staged key is discarded.
    """
    staged = provider.read_staged_key()
    if staged is None:
        return
    if key_fingerprint(staged) == recorded_fingerprint:
        provider.promote_staged_key()
        logger.warning(
            "finished an interrupted encryption key rotation (new key %s)",
            fingerprint_prefix(recorded_fingerprint),
        )
        return
    if provider.fingerprint() == recorded_fingerprint:
        provider.discard_staged_key()
        logger.warning("discarded the staged key of a rotation that did not commit")


def _refusal(problem: str, provider: KeyProvider, expected: str | None) -> str:
    """The operator-facing refusal text (no key material)."""
    expected_text = (
        f" The database was encrypted with key {fingerprint_prefix(expected)}."
        if expected
        else ""
    )
    return (
        f"Refusing to start: {problem} ({provider.describe()}).{expected_text} "
        "Stored credentials cannot be read without the original key. To recover: "
        "(1) restore the original key from a backup and start the service again; "
        "or (2) if the key is lost for good, stop the service and run "
        "'timelapse-manager secrets reset --confirm', which removes every "
        "stored camera password, the default camera credentials, the SMTP "
        "password, the webhook signing secret and the LDAP bind password "
        "(sign in with a local admin account, then enter them again). "
        "'timelapse-manager secrets status' shows the current state."
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _check_sync(
    settings: Settings, session_factory: sessionmaker[Session]
) -> KeyProvider:
    """The blocking body of :func:`ensure_key_integrity` (runs in a worker thread)."""
    with session_factory() as session:
        try:
            row = session.get(SecretsMeta, 1)
        except OperationalError:
            # The service migrates to head before it starts, so this is an app
            # built over an unmigrated database (tests, tooling). There is no
            # stored ciphertext to protect, so behave as before the check.
            session.rollback()
            logger.warning("secrets_meta is missing; the key was not checked")
            return build_key_provider(settings.secrets, settings.paths.data_dir)
        if row is None:
            selected = build_key_provider(settings.secrets, settings.paths.data_dir)
            _record_first_time(session, selected)
            session.commit()
            return selected
        recorded = provider_for_name(
            row.provider, settings.secrets, settings.paths.data_dir
        )
        if recorded is None:
            raise KeyIntegrityError(
                f"Refusing to start: the encryption key was recorded in the "
                f"'{row.provider}' store, which is not available now. Make "
                "that store reachable again, or stop the service and run "
                "'timelapse-manager secrets reset --confirm' (removes every "
                "stored credential; see 'secrets status')."
            )
        _verify_recorded(session, recorded, row)
        session.commit()
        return recorded


def _record_first_time(session: Session, provider: KeyProvider) -> None:
    """No row yet: prove the key reads the data (or that there is none), then record."""
    samples = sample_ciphertexts(session)
    if samples:
        key = provider.load_existing_key()
        if key is None:
            raise KeyIntegrityError(
                _refusal("the encryption key is missing", provider, None)
            )
        cipher = Fernet(key)
        if not all(decrypts_with(cipher, sample) for sample in samples):
            raise KeyIntegrityError(
                _refusal(
                    "the encryption key does not decrypt the stored credentials",
                    provider,
                    None,
                )
            )
        fingerprint = key_fingerprint(key)
    else:
        provider.get_cipher()  # creates the key, exclusively, when absent
        fingerprint = provider.fingerprint() or ""
    session.add(SecretsMeta(id=1, provider=provider.name, key_fingerprint=fingerprint))
    logger.info(
        "recorded the encryption key (provider %s, fingerprint %s)",
        provider.name,
        fingerprint_prefix(fingerprint),
    )


def _verify_recorded(session: Session, provider: KeyProvider, row: SecretsMeta) -> None:
    """A row exists: the loaded key must be the recorded one."""
    recover_staged_key(provider, row.key_fingerprint)
    ciphertext = has_ciphertext(session)
    key = provider.load_existing_key()
    if key is None:
        if ciphertext:
            raise KeyIntegrityError(
                _refusal("the encryption key is missing", provider, row.key_fingerprint)
            )
        # Nothing is encrypted, so nothing is lost by starting a fresh key.
        provider.get_cipher()
        _rerecord(row, provider.fingerprint() or "", "the key was missing")
        return
    fingerprint = key_fingerprint(key)
    if fingerprint == row.key_fingerprint:
        return
    if ciphertext:
        raise KeyIntegrityError(
            _refusal(
                f"the encryption key (fingerprint {fingerprint_prefix(fingerprint)}) "
                "is not the recorded key",
                provider,
                row.key_fingerprint,
            )
        )
    _rerecord(row, fingerprint, "the key changed")


def _rerecord(row: SecretsMeta, fingerprint: str, why: str) -> None:
    """Update the record when no ciphertext depends on the old one."""
    row.key_fingerprint = fingerprint
    row.rotated_at = _now()
    logger.warning(
        "%s and no encrypted secrets are stored; recorded key %s",
        why,
        fingerprint_prefix(fingerprint),
    )


async def ensure_key_integrity(
    settings: Settings, session_factory: sessionmaker[Session]
) -> KeyProvider:
    """Check the key against the record and return the provider to install.

    Runs the blocking work in a worker thread. On a refusal it logs the
    operator message once at CRITICAL, flags the process (see
    :func:`key_integrity_refused`) and raises :class:`KeyIntegrityError`, which
    fails the lifespan startup.
    """
    global _refused
    try:
        try:
            return await asyncio.to_thread(_check_sync, settings, session_factory)
        except (OSError, ValueError) as exc:
            # An unreadable, over-permissive or malformed key file: the same
            # class of fault, for the operator to fix. No key bytes in ``exc``.
            raise KeyIntegrityError(
                f"Refusing to start: the encryption key cannot be loaded: {exc}"
            ) from exc
    except KeyIntegrityError as exc:
        _refused = True
        logger.critical("%s", exc)
        raise

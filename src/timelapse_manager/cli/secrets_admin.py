"""``secrets`` subcommands: inspect, rotate and reset the at-rest key (#67 S8).

Kept apart from :mod:`timelapse_manager.cli` so the command bodies are plain
functions of ``(settings, args)``. They run **offline**, against the database
and the key store, and the ``rotate`` and ``reset`` commands refuse while the
service answers on loopback (the caller passes ``service_running``). The caller
also runs them as the owner of the data directory, so a key file or SQLite
sidecar created here is not root-owned and unreadable to the service.

Nothing here prints key material: only the provider, the key's location, an
8-character fingerprint prefix and counts.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.exc import SQLAlchemyError

from ..db.engine import create_db_engine
from ..db.models import SecretsMeta
from ..db.session import create_session_factory
from ..security.key_integrity import (
    clear_all_ciphertext,
    count_ciphertexts,
    decrypts_with,
    recover_staged_key,
    reencrypt_all,
    sample_ciphertexts,
)
from ..security.keystore import (
    KeyFileProvider,
    KeyProvider,
    _default_key_file,
    fingerprint_prefix,
    key_fingerprint,
    provider_for_name,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

    from ..config import Settings

_EXIT_OK = 0
_EXIT_ERROR = 1


def add_commands(subparsers: Any) -> None:
    """Register the ``secrets`` subcommands."""
    secrets_parser = subparsers.add_parser(
        "secrets", help="Inspect and manage the at-rest encryption key."
    )
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_command", required=True)
    secrets_sub.add_parser(
        "status",
        help=(
            "Show the key provider, a fingerprint prefix and whether the key "
            "matches the stored secrets. Prints no key material."
        ),
    )
    secrets_sub.add_parser(
        "rotate",
        help=(
            "Replace the key and re-encrypt every stored secret. The service "
            "must be stopped. Back up the data directory first."
        ),
    )
    reset = secrets_sub.add_parser(
        "reset",
        help=(
            "Recovery for a lost key: remove every stored secret and record a "
            "new key. The service must be stopped."
        ),
    )
    reset.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Acknowledges that stored passwords are deleted.",
    )


def recorded_provider(
    settings: Settings, session: Session
) -> tuple[SecretsMeta | None, KeyProvider | None]:
    """The recorded row and its provider; the file provider when nothing is recorded.

    The CLI never probes the keyring for a fresh install: it runs as root, whose
    keyring is not the service's.
    """
    data_dir = settings.paths.data_dir
    row = session.get(SecretsMeta, 1)
    if row is None:
        return None, KeyFileProvider(_default_key_file(settings.secrets, data_dir))
    return row, provider_for_name(row.provider, settings.secrets, data_dir)


def existing_key_provider(settings: Settings) -> KeyProvider | None:
    """The provider whose key already exists, or ``None`` (never creates a key)."""
    engine = create_db_engine(settings.database.url)
    try:
        with create_session_factory(engine)() as session:
            try:
                _row, provider = recorded_provider(settings, session)
            except SQLAlchemyError:
                # No ``secrets_meta`` table yet (database older than 040).
                provider = KeyFileProvider(
                    _default_key_file(settings.secrets, settings.paths.data_dir)
                )
    finally:
        engine.dispose()
    if provider is None or provider.load_existing_key() is None:
        return None
    return provider


def _mode_text(provider: KeyProvider) -> str:
    """The key file's permission bits, for a file provider that has one."""
    if provider.name != KeyFileProvider.name or os.name == "nt":
        return ""
    try:
        mode = stat.S_IMODE(os.stat(provider.describe()).st_mode)
    except OSError:
        return ""
    return f" (mode {mode:#o})"


def _status(settings: Settings, factory: sessionmaker[Session]) -> int:
    with factory() as session:
        row, provider = recorded_provider(settings, session)
        counts = count_ciphertexts(session)
        samples = sample_ciphertexts(session)
        if provider is None:
            assert row is not None
            print(f"provider: {row.provider} (recorded; NOT AVAILABLE now)")
            print("state: PROVIDER UNAVAILABLE")
            return _EXIT_ERROR
        key = provider.load_existing_key()
        print(f"provider: {provider.name}")
        print(f"key: {provider.describe()}{_mode_text(provider)}")
        recorded = row.key_fingerprint if row is not None else None
        print(
            "recorded fingerprint: "
            + (fingerprint_prefix(recorded) if recorded else "(none yet)")
        )
        current = key_fingerprint(key) if key is not None else None
        print(
            "current fingerprint: "
            + (fingerprint_prefix(current) if current else "(key not found)")
        )
        print(f"encrypted values: {sum(counts.values())}")
        for label, total in sorted(counts.items()):
            print(f"  {label}: {total}")
        print(f"staged key: {'present' if provider.read_staged_key() else 'none'}")
        if key is None:
            state, ok = (
                ("KEY MISSING" if counts else "no key yet (none needed)"),
                (not counts),
            )
        elif recorded is not None:
            matches = current == recorded
            state, ok = ("ok" if matches else "KEY MISMATCH"), matches
        elif samples and not all(decrypts_with(Fernet(key), s) for s in samples):
            state, ok = "KEY DOES NOT DECRYPT THE STORED SECRETS", False
        else:
            state, ok = "ok (not recorded yet; recorded at the next start)", True
        print(f"state: {state}")
        return _EXIT_OK if ok else _EXIT_ERROR


def _rotate(settings: Settings, factory: sessionmaker[Session]) -> int:
    with factory() as session:
        row, provider = recorded_provider(settings, session)
        if row is None:
            print(
                "error: no key is recorded yet; start the service once, then "
                "stop it and rotate",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        if provider is None:
            print(
                f"error: the recorded key store ({row.provider}) is not available",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        recover_staged_key(provider, row.key_fingerprint)
        if provider.fingerprint() != row.key_fingerprint:
            print(
                "error: the key does not match the recorded fingerprint; run "
                "'timelapse-manager secrets status'",
                file=sys.stderr,
            )
            return _EXIT_ERROR
        recorded_fingerprint = row.key_fingerprint
    reencrypted = 0

    def reencrypt(old: Fernet, new: Fernet) -> None:
        nonlocal reencrypted
        with factory() as session:
            reencrypted = reencrypt_all(session, old, new)
            meta = session.get(SecretsMeta, 1)
            assert meta is not None
            meta.key_fingerprint = provider.pending_fingerprint()
            meta.rotated_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()

    try:
        provider.rotate(reencrypt)
    except InvalidToken:
        print(
            "error: a stored secret could not be decrypted with the current key; "
            "nothing was changed. Run 'timelapse-manager secrets status'.",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    print(
        f"rotated: {reencrypted} stored secrets re-encrypted; key "
        f"{fingerprint_prefix(recorded_fingerprint)} -> "
        f"{fingerprint_prefix(provider.fingerprint() or '')}"
    )
    print("Back up the new key, then start the service.")
    return _EXIT_OK


def _reset(settings: Settings, factory: sessionmaker[Session]) -> int:
    key = Fernet.generate_key()
    with factory() as session:
        row, provider = recorded_provider(settings, session)
        if provider is None:
            provider = KeyFileProvider(
                _default_key_file(settings.secrets, settings.paths.data_dir)
            )
        removed = clear_all_ciphertext(session)
        if row is None:
            row = SecretsMeta(id=1, provider=provider.name, key_fingerprint="")
            session.add(row)
        row.provider = provider.name
        row.key_fingerprint = key_fingerprint(key)
        row.rotated_at = datetime.now(UTC).replace(tzinfo=None)
        # The database first: if the key write below is interrupted, the next
        # start finds no ciphertext and re-records whatever key is there.
        session.commit()
    provider.discard_staged_key()
    provider.replace_key(key)
    print(
        f"reset: {removed} stored secrets removed; new key "
        f"{fingerprint_prefix(key_fingerprint(key))} recorded at {provider.describe()}"
    )
    print(
        "Start the service, sign in with a local admin account, and re-enter "
        "the camera passwords, SMTP, webhook and LDAP bind settings."
    )
    return _EXIT_OK


def run(settings: Settings, args: argparse.Namespace, *, service_running: bool) -> int:
    """Run one ``secrets`` subcommand; return the process exit status."""
    command = args.secrets_command
    if command == "reset" and not args.confirm:
        print(
            "error: 'secrets reset' deletes every stored password; "
            "pass --confirm to proceed",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    if command in ("rotate", "reset") and service_running:
        print(
            "error: the service is answering on loopback; stop it first "
            "(systemctl stop timelapse-manager)",
            file=sys.stderr,
        )
        return _EXIT_ERROR
    engine = create_db_engine(settings.database.url)
    try:
        factory = create_session_factory(engine)
        try:
            if command == "status":
                return _status(settings, factory)
            if command == "rotate":
                return _rotate(settings, factory)
            if command == "reset":
                return _reset(settings, factory)
        except SQLAlchemyError as exc:
            print(
                f"error: the database could not be read ({type(exc).__name__}); "
                "has 'timelapse-manager migrate' been run?",
                file=sys.stderr,
            )
            return _EXIT_ERROR
    finally:
        engine.dispose()
    return _EXIT_ERROR

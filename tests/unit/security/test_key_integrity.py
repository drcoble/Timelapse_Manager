"""Startup key-integrity check and the ``secrets`` CLI (#67 S8).

Covers the boot branches (fresh install, upgrade of an install with encrypted
data, missing / wrong / moved key, interrupted rotation), the registry of
encrypted columns, and ``secrets status|rotate|reset``. The keyring is faked
wherever a test needs the OS-keystore provider.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

import keyring
import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import timelapse_manager.cli as cli_mod
from tests.conftest import LOOPBACK_PEER
from timelapse_manager.app import create_app
from timelapse_manager.cli import main
from timelapse_manager.config import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import (
    Base,
    Camera,
    CameraDefaultCredentials,
    LdapSettings,
    NotificationSettings,
    SecretsMeta,
)
from timelapse_manager.db.session import create_session_factory
from timelapse_manager.security import key_integrity
from timelapse_manager.security.crypto import (
    decrypt_secret,
    encrypt_credentials,
    encrypt_secret,
    set_key_provider,
)
from timelapse_manager.security.key_integrity import (
    EXIT_KEY_INTEGRITY,
    KeyIntegrityError,
    _check_sync,
    count_ciphertexts,
    ensure_key_integrity,
    key_integrity_refused,
    reset_key_integrity_refusal,
)
from timelapse_manager.security.keystore import (
    KeyFileProvider,
    OsKeystoreProvider,
    key_fingerprint,
)


@pytest.fixture
def factory(settings: Settings) -> sessionmaker[Session]:
    """A session factory over a database with the full schema."""
    engine = create_db_engine(settings.database.url)
    Base.metadata.create_all(engine)
    yield_factory = create_session_factory(engine)
    return yield_factory


@pytest.fixture(autouse=True)
def _clean_refusal() -> None:
    reset_key_integrity_refusal()


def _key_file(settings: Settings) -> Path:
    return settings.paths.data_dir / ".secret-key"


def _seed_ciphertext(
    factory: sessionmaker[Session], key_file: Path, *, columns: str = "all"
) -> None:
    """Store encrypted values in every secret column, under the key in ``key_file``."""
    provider = KeyFileProvider(key_file)
    set_key_provider(provider)
    with factory() as session:
        session.add(
            Camera(
                name="cam",
                credentials=encrypt_credentials(
                    {"username": "u", "password": "camera-pw"}
                ),
            )
        )
        if columns == "all":
            session.add(
                CameraDefaultCredentials(
                    id=1, username="d", password=encrypt_secret("default-pw")
                )
            )
            session.add(LdapSettings(id=1, bind_password=encrypt_secret("bind-pw")))
            session.add(
                NotificationSettings(
                    id=1,
                    smtp_password=encrypt_secret("smtp-pw"),
                    webhook_signing_secret=encrypt_secret("hook-secret"),
                )
            )
        session.commit()


def _row(factory: sessionmaker[Session]) -> SecretsMeta | None:
    with factory() as session:
        return session.get(SecretsMeta, 1)


class TestFirstBoot:
    def test_fresh_install_creates_one_key_and_records_it(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        provider = _check_sync(settings, factory)

        key = _key_file(settings).read_bytes()
        row = _row(factory)
        assert row is not None
        assert row.provider == "file"
        assert row.key_fingerprint == key_fingerprint(key)
        assert provider.fingerprint() == row.key_fingerprint

    def test_upgrade_with_existing_encrypted_data_records_without_lockout(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        """An install from before the check: ciphertext, key, no row."""
        _seed_ciphertext(factory, _key_file(settings))
        key_before = _key_file(settings).read_bytes()
        assert _row(factory) is None

        provider = _check_sync(settings, factory)
        set_key_provider(provider)

        row = _row(factory)
        assert row is not None
        assert row.key_fingerprint == key_fingerprint(key_before)
        assert _key_file(settings).read_bytes() == key_before
        with factory() as session:
            assert (
                decrypt_secret(
                    session.execute(select(LdapSettings.bind_password)).scalar_one()
                )
                == "bind-pw"
            )

    def test_legacy_plaintext_does_not_count_as_ciphertext(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        with factory() as session:
            session.add(Camera(name="old", credentials={"password": "plain"}))
            session.add(NotificationSettings(id=1, smtp_password="plain-smtp"))
            session.commit()

        # No key file and nothing encrypted: a key is created, nothing refused.
        _check_sync(settings, factory)

        assert _key_file(settings).exists()
        assert _row(factory) is not None

    def test_missing_key_with_ciphertext_and_no_row_refuses_and_mints_nothing(
        self, settings: Settings, factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        _seed_ciphertext(factory, tmp_path / "elsewhere-key")
        assert not _key_file(settings).exists()

        with pytest.raises(KeyIntegrityError, match="key is missing"):
            _check_sync(settings, factory)

        assert not _key_file(settings).exists()
        assert _row(factory) is None

    def test_wrong_key_with_ciphertext_and_no_row_refuses(
        self, settings: Settings, factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        _seed_ciphertext(factory, tmp_path / "elsewhere-key")
        KeyFileProvider(_key_file(settings)).get_cipher()  # a different key

        with pytest.raises(KeyIntegrityError, match="does not decrypt"):
            _check_sync(settings, factory)

        assert _row(factory) is None


class TestRecordedKey:
    def _boot_with_data(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> SecretsMeta:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        row = _row(factory)
        assert row is not None
        return row

    def test_matching_key_starts(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        self._boot_with_data(settings, factory)
        _check_sync(settings, factory)

    def test_deleted_key_file_is_refused_not_replaced(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        row = self._boot_with_data(settings, factory)
        _key_file(settings).unlink()

        with pytest.raises(KeyIntegrityError) as excinfo:
            _check_sync(settings, factory)

        assert not _key_file(settings).exists()
        message = str(excinfo.value)
        assert str(_key_file(settings)) in message
        assert row.key_fingerprint[:8] in message
        assert row.key_fingerprint not in message
        assert "secrets reset --confirm" in message

    def test_changed_key_is_refused_and_leaves_the_record_alone(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        row = self._boot_with_data(settings, factory)
        recorded = row.key_fingerprint
        _key_file(settings).write_bytes(Fernet.generate_key())
        _key_file(settings).chmod(0o600)

        with pytest.raises(KeyIntegrityError, match="not the recorded key"):
            _check_sync(settings, factory)

        row_after = _row(factory)
        assert row_after is not None
        assert row_after.key_fingerprint == recorded

    def test_changed_key_without_ciphertext_is_re_recorded(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        _check_sync(settings, factory)
        new_key = Fernet.generate_key()
        _key_file(settings).write_bytes(new_key)
        _key_file(settings).chmod(0o600)

        _check_sync(settings, factory)

        row = _row(factory)
        assert row is not None
        assert row.key_fingerprint == key_fingerprint(new_key)

    def test_missing_key_without_ciphertext_starts_a_fresh_one(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        _check_sync(settings, factory)
        _key_file(settings).unlink()

        _check_sync(settings, factory)

        row = _row(factory)
        assert row is not None
        assert row.key_fingerprint == key_fingerprint(_key_file(settings).read_bytes())

    def test_recorded_provider_is_used_not_a_fresh_probe(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Row says ``file``; a keyring that now round-trips must not take over."""
        self._boot_with_data(settings, factory)
        from timelapse_manager.security import keystore

        monkeypatch.setattr(keystore, "_os_keystore_available", lambda _name: True)
        wants_keyring = settings.model_copy(
            update={
                "secrets": settings.secrets.model_copy(update={"use_os_keystore": True})
            }
        )

        provider = _check_sync(wants_keyring, factory)

        assert provider.name == "file"

    def test_recorded_keystore_that_is_gone_is_refused(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._boot_with_data(settings, factory)
        with factory() as session:
            meta = session.get(SecretsMeta, 1)
            assert meta is not None
            meta.provider = "os_keystore"
            session.commit()
        from timelapse_manager.security import keystore

        monkeypatch.setattr(keystore, "_os_keystore_available", lambda _name: False)

        with pytest.raises(KeyIntegrityError, match="not available"):
            _check_sync(settings, factory)


class TestInterruptedRotation:
    def _staged(
        self, settings: Settings, factory: sessionmaker[Session], *, committed: bool
    ) -> tuple[bytes, bytes]:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        old_key = _key_file(settings).read_bytes()
        new_key = Fernet.generate_key()
        staged = _key_file(settings).with_name(".secret-key.next")
        staged.write_bytes(new_key)
        staged.chmod(0o600)
        if committed:
            old_cipher, new_cipher = Fernet(old_key), Fernet(new_key)
            with factory() as session:
                key_integrity.reencrypt_all(session, old_cipher, new_cipher)
                meta = session.get(SecretsMeta, 1)
                assert meta is not None
                meta.key_fingerprint = key_fingerprint(new_key)
                session.commit()
        return old_key, new_key

    def test_crash_between_commit_and_rename_is_recovered_on_boot(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        _old, new_key = self._staged(settings, factory, committed=True)

        provider = _check_sync(settings, factory)
        set_key_provider(provider)

        assert _key_file(settings).read_bytes() == new_key
        assert not _key_file(settings).with_name(".secret-key.next").exists()
        with factory() as session:
            assert (
                decrypt_secret(
                    session.execute(select(LdapSettings.bind_password)).scalar_one()
                )
                == "bind-pw"
            )

    def test_staged_key_of_a_rotation_that_never_committed_is_discarded(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        old_key, _new = self._staged(settings, factory, committed=False)

        _check_sync(settings, factory)

        assert _key_file(settings).read_bytes() == old_key
        assert not _key_file(settings).with_name(".secret-key.next").exists()


class TestRefusalReporting:
    def test_refusal_flags_the_process_and_logs_no_key(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        key = _key_file(settings).read_bytes()
        _key_file(settings).unlink()

        with (
            caplog.at_level(logging.DEBUG),
            pytest.raises(KeyIntegrityError),
        ):
            asyncio.run(ensure_key_integrity(settings, factory))

        assert key_integrity_refused()
        assert key.decode("ascii") not in caplog.text
        assert key_fingerprint(key) not in caplog.text
        assert EXIT_KEY_INTEGRITY == 78

    def test_over_permissive_key_file_is_a_refusal(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        _check_sync(settings, factory)
        _key_file(settings).chmod(0o644)

        with pytest.raises(KeyIntegrityError, match="chmod 600"):
            asyncio.run(ensure_key_integrity(settings, factory))
        assert key_integrity_refused()


class TestLifespan:
    def test_app_start_records_the_key(self, settings: Settings) -> None:
        engine = create_db_engine(settings.database.url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)

        with TestClient(create_app(settings), client=LOOPBACK_PEER):
            assert _row(factory) is not None

    def test_app_refuses_to_start_on_a_key_mismatch(self, settings: Settings) -> None:
        engine = create_db_engine(settings.database.url)
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        _key_file(settings).unlink()

        with (
            pytest.raises(KeyIntegrityError),
            TestClient(create_app(settings), client=LOOPBACK_PEER),
        ):
            pass
        assert key_integrity_refused()
        assert not _key_file(settings).exists()


class TestConcurrentCreation:
    def test_ten_threads_with_separate_providers_get_one_key(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / ".secret-key"
        barrier = threading.Barrier(10)
        fingerprints: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                provider = KeyFileProvider(key_file)
                barrier.wait()
                provider.get_cipher()
                with lock:
                    fingerprints.append(provider.fingerprint() or "")
            except BaseException as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        assert len(set(fingerprints)) == 1
        assert key_fingerprint(key_file.read_bytes()) == fingerprints[0]

    def test_empty_key_file_is_absent_not_a_key(self, tmp_path: Path) -> None:
        """#189: a file created but not yet written is not the key ``b""``."""
        key_file = tmp_path / ".secret-key"
        key_file.touch(mode=0o600)
        provider = KeyFileProvider(key_file)
        assert provider.load_existing_key() is None
        assert provider.fingerprint() is None

    def test_reader_waits_for_a_key_file_that_is_created_but_not_written(
        self, tmp_path: Path
    ) -> None:
        """The winner has created the file and not yet written: wait, do not fail.

        The write is made to land on the loser's second read, so the loser is
        provably mid-wait; no timing is involved.
        """
        key_file = tmp_path / ".secret-key"
        key_file.touch(mode=0o600)
        key = Fernet.generate_key()
        provider = KeyFileProvider(key_file)
        reads = 0
        original_read = provider._read_key

        def read_key() -> bytes | None:
            nonlocal reads
            reads += 1
            if reads == 2:
                key_file.write_bytes(key)
            return original_read()

        provider._read_key = read_key  # type: ignore[method-assign]

        assert provider.get_cipher().decrypt(Fernet(key).encrypt(b"x")) == b"x"
        assert reads >= 2

    def test_create_never_overwrites_an_existing_key(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        key_file.write_bytes(b"existing")
        key_file.chmod(0o600)

        with pytest.raises(FileExistsError):
            KeyFileProvider._write_exclusive(key_file, b"other")
        assert key_file.read_bytes() == b"existing"

    def test_non_creating_load_does_not_create(self, tmp_path: Path) -> None:
        provider = KeyFileProvider(tmp_path / ".secret-key")
        assert provider.load_existing_key() is None
        assert provider.fingerprint() is None
        assert not (tmp_path / ".secret-key").exists()


class TestRotation:
    def test_rotate_stages_then_promotes_and_drops_the_cached_cipher(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        old_cipher = provider.get_cipher()
        old_fingerprint = provider.fingerprint()
        seen: dict[str, object] = {}

        def reencrypt(_old: Fernet, _new: Fernet) -> None:
            seen["stored_during"] = key_file.read_bytes()
            seen["staged_during"] = provider.pending_fingerprint()

        provider.rotate(reencrypt)

        assert key_fingerprint(seen["stored_during"]) == old_fingerprint  # type: ignore[arg-type]
        assert provider.fingerprint() == seen["staged_during"]
        assert provider.get_cipher() is not old_cipher
        assert not key_file.with_name(".secret-key.next").exists()

    def test_failed_callback_discards_the_staged_key(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        provider.get_cipher()
        before = key_file.read_bytes()

        def failing(_old: Fernet, _new: Fernet) -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            provider.rotate(failing)

        assert key_file.read_bytes() == before
        assert not key_file.with_name(".secret-key.next").exists()

    def test_leftover_staged_key_blocks_a_second_rotation(self, tmp_path: Path) -> None:
        key_file = tmp_path / ".secret-key"
        provider = KeyFileProvider(key_file)
        provider.get_cipher()
        key_file.with_name(".secret-key.next").write_bytes(b"leftover")

        with pytest.raises(FileExistsError):
            provider.rotate(lambda _old, _new: None)

        assert key_file.with_name(".secret-key.next").read_bytes() == b"leftover"


# ---------------------------------------------------------------------------
# The ``secrets`` subcommands
# ---------------------------------------------------------------------------


def _run(settings: Settings, *args: str) -> int:
    config = settings.paths.data_dir.parent / "config.yml"
    import yaml

    config.write_text(
        yaml.dump(
            {
                "paths": {
                    "data_dir": str(settings.paths.data_dir),
                    "frames_root": str(settings.paths.frames_root),
                    "token_file": str(settings.paths.token_file),
                },
                "database": {"url": settings.database.url},
                "secrets": {"use_os_keystore": False},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as excinfo:
        main(["--config", str(config), *args])
    code = excinfo.value.code
    return int(code) if code is not None else 0


@pytest.fixture
def service_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "_service_answers", lambda _settings: False)


class TestSecretsStatus:
    def test_status_ok_prints_no_key_material(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        key = _key_file(settings).read_bytes()

        code = _run(settings, "secrets", "status")

        out = capsys.readouterr().out
        assert code == 0
        assert "provider: file" in out
        assert key_fingerprint(key)[:8] in out
        assert key_fingerprint(key) not in out
        assert key.decode("ascii") not in out
        assert "state: ok" in out
        assert "encrypted values: 5" in out

    def test_status_reports_a_missing_key_with_a_failing_exit(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        _key_file(settings).unlink()

        code = _run(settings, "secrets", "status")

        assert code == 1
        assert "KEY MISSING" in capsys.readouterr().out


class TestSecretsRotate:
    def test_rotate_re_encrypts_everything_and_old_ciphertext_stops_decrypting(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        service_stopped: None,
    ) -> None:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        old_key = _key_file(settings).read_bytes()
        with factory() as session:
            old_token = session.execute(
                select(NotificationSettings.smtp_password)
            ).scalar_one()

        assert _run(settings, "secrets", "rotate") == 0

        new_key = _key_file(settings).read_bytes()
        assert new_key != old_key
        row = _row(factory)
        assert row is not None
        assert row.key_fingerprint == key_fingerprint(new_key)
        assert row.rotated_at is not None
        with factory() as session:
            assert count_ciphertexts(session)
            new_token = session.execute(
                select(NotificationSettings.smtp_password)
            ).scalar_one()
            creds = session.execute(select(Camera.credentials)).scalar_one()
        new_cipher = Fernet(new_key)
        assert (
            new_cipher.decrypt(new_token.removeprefix("enc:v1:").encode()) == b"smtp-pw"
        )
        assert (
            new_cipher.decrypt(creds["password"].removeprefix("enc:v1:").encode())
            == b"camera-pw"
        )
        assert creds["username"] == "u"
        with pytest.raises(InvalidToken):
            new_cipher.decrypt(old_token.removeprefix("enc:v1:").encode())
        # The next start accepts the rotated key.
        _check_sync(settings, factory)

    def test_rotate_refuses_while_the_service_answers(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _check_sync(settings, factory)
        before = _key_file(settings).read_bytes()
        monkeypatch.setattr(cli_mod, "_service_answers", lambda _settings: True)

        assert _run(settings, "secrets", "rotate") == 1
        assert _key_file(settings).read_bytes() == before

    def test_rotate_leaves_everything_alone_when_a_value_cannot_be_decrypted(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        service_stopped: None,
        tmp_path: Path,
    ) -> None:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        with factory() as session:
            other = tmp_path / "other-key"
            set_key_provider(KeyFileProvider(other))
            meta = session.get(LdapSettings, 1)
            assert meta is not None
            meta.bind_password = encrypt_secret("from-another-key")
            session.commit()
        key_before = _key_file(settings).read_bytes()

        assert _run(settings, "secrets", "rotate") == 1

        assert _key_file(settings).read_bytes() == key_before
        assert not _key_file(settings).with_name(".secret-key.next").exists()


class TestSecretsReset:
    def test_reset_needs_confirm(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        service_stopped: None,
    ) -> None:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)

        assert _run(settings, "secrets", "reset") == 1

        with factory() as session:
            assert count_ciphertexts(session)

    def test_reset_after_key_loss_clears_secrets_and_the_next_boot_succeeds(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        service_stopped: None,
    ) -> None:
        _seed_ciphertext(factory, _key_file(settings))
        _check_sync(settings, factory)
        _key_file(settings).unlink()
        with pytest.raises(KeyIntegrityError):
            _check_sync(settings, factory)

        assert _run(settings, "secrets", "reset", "--confirm") == 0

        with factory() as session:
            assert count_ciphertexts(session) == {}
            creds = session.execute(select(Camera.credentials)).scalar_one()
            assert creds == {"username": "u"}
        assert _key_file(settings).stat().st_mode & 0o077 == 0
        _check_sync(settings, factory)

    def test_reset_keeps_the_replaced_key_file(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        service_stopped: None,
    ) -> None:
        _check_sync(settings, factory)
        old = _key_file(settings).read_bytes()

        assert _run(settings, "secrets", "reset", "--confirm") == 0

        kept = list(settings.paths.data_dir.glob(".secret-key.replaced-*"))
        assert len(kept) == 1
        assert kept[0].read_bytes() == old
        assert _key_file(settings).read_bytes() != old

    def test_reset_refuses_while_the_service_answers(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _check_sync(settings, factory)
        monkeypatch.setattr(cli_mod, "_service_answers", lambda _settings: True)

        assert _run(settings, "secrets", "reset", "--confirm") == 1


class TestCliUsesTheRecordedProvider:
    def test_existing_key_provider_follows_the_row(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from timelapse_manager.cli import secrets_admin
        from timelapse_manager.security import keystore

        store: dict[tuple[str, str], str] = {}
        monkeypatch.setattr(
            keyring,
            "get_password",
            lambda service, account: store.get((service, account)),
        )
        monkeypatch.setattr(
            keyring,
            "set_password",
            lambda service, account, value: store.__setitem__(
                (service, account), value
            ),
        )
        monkeypatch.setattr(keystore, "_os_keystore_available", lambda _name: True)
        service = settings.secrets.keystore_service_name
        key = Fernet.generate_key()
        store[(service, OsKeystoreProvider._ACCOUNT)] = key.decode("ascii")
        with factory() as session:
            session.add(
                SecretsMeta(
                    id=1, provider="os_keystore", key_fingerprint=key_fingerprint(key)
                )
            )
            session.commit()

        provider = secrets_admin.existing_key_provider(settings)

        assert provider is not None
        assert provider.name == "os_keystore"
        assert provider.fingerprint() == key_fingerprint(key)

    def test_existing_key_provider_never_creates_a_key(
        self, settings: Settings, factory: sessionmaker[Session]
    ) -> None:
        from timelapse_manager.cli import secrets_admin

        assert secrets_admin.existing_key_provider(settings) is None
        assert not _key_file(settings).exists()

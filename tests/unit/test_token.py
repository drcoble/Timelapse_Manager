"""Tests for local bearer-token authentication."""

from __future__ import annotations

import stat
from pathlib import Path

from fastapi.testclient import TestClient

from timelapse_manager.config.settings import Settings
from timelapse_manager.security.token import ensure_local_token, verify_token

# ---------------------------------------------------------------------------
# ensure_local_token: file creation and idempotency
# ---------------------------------------------------------------------------


class TestEnsureLocalToken:
    def test_creates_token_file_on_first_call(self, settings: Settings) -> None:
        token_file = settings.paths.token_file
        assert token_file is not None
        assert not token_file.exists()
        ensure_local_token(settings)
        assert token_file.exists()

    def test_token_file_is_non_empty(self, settings: Settings) -> None:
        ensure_local_token(settings)
        assert settings.paths.token_file is not None  # type: ignore[union-attr]
        content = settings.paths.token_file.read_text(encoding="utf-8").strip()
        assert content != ""

    def test_token_is_returned_on_creation(self, settings: Settings) -> None:
        token = ensure_local_token(settings)
        assert isinstance(token, str)
        assert token.strip() != ""

    def test_token_file_has_owner_only_permissions(self, settings: Settings) -> None:
        ensure_local_token(settings)
        assert settings.paths.token_file is not None  # type: ignore[union-attr]
        mode = settings.paths.token_file.stat().st_mode
        # Only owner read/write bits (0o600) should be set for non-owner.
        group_world_bits = mode & (stat.S_IRWXG | stat.S_IRWXO)
        assert group_world_bits == 0, f"Token file has loose permissions: {oct(mode)}"

    def test_returns_existing_token_on_second_call(self, settings: Settings) -> None:
        token_a = ensure_local_token(settings)
        token_b = ensure_local_token(settings)
        assert token_a == token_b

    def test_does_not_overwrite_existing_file(self, settings: Settings) -> None:
        token_a = ensure_local_token(settings)
        assert settings.paths.token_file is not None  # type: ignore[union-attr]
        mtime_a = settings.paths.token_file.stat().st_mtime
        ensure_local_token(settings)
        mtime_b = settings.paths.token_file.stat().st_mtime
        # mtime should not change if we didn't write.
        assert mtime_a == mtime_b
        assert ensure_local_token(settings) == token_a

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        from timelapse_manager.config.settings import (
            DatabaseSettings,
            PathsSettings,
        )

        deep_token = tmp_path / "nested" / "deep" / ".token"
        s = Settings(
            database=DatabaseSettings(url="sqlite:///./test.db"),
            paths=PathsSettings(
                data_dir=tmp_path / "data",
                token_file=deep_token,
            ),
        )
        ensure_local_token(s)
        assert deep_token.exists()

    def test_token_is_64_hex_characters(self, settings: Settings) -> None:
        """32 bytes of token_hex produces 64 hex characters."""
        token = ensure_local_token(settings)
        assert len(token) == 64
        assert all(c in "0123456789abcdef" for c in token)


# ---------------------------------------------------------------------------
# verify_token
# ---------------------------------------------------------------------------


class TestVerifyToken:
    def test_matching_tokens_returns_true(self) -> None:
        assert verify_token("abc123", "abc123") is True

    def test_mismatched_tokens_returns_false(self) -> None:
        assert verify_token("abc123", "xyz789") is False

    def test_empty_expected_always_returns_false(self) -> None:
        assert verify_token("abc123", "") is False

    def test_empty_received_returns_false(self) -> None:
        assert verify_token("", "abc123") is False

    def test_both_empty_returns_false(self) -> None:
        assert verify_token("", "") is False

    def test_partial_match_returns_false(self) -> None:
        assert verify_token("abc", "abc123") is False

    def test_case_sensitive(self) -> None:
        assert verify_token("ABC123", "abc123") is False


# ---------------------------------------------------------------------------
# require_local_token dependency (via the live API)
# ---------------------------------------------------------------------------


class TestRequireLocalTokenDependency:
    def test_missing_auth_header_returns_401(self, client: TestClient) -> None:
        response = client.get("/api/v1/system")
        assert response.status_code == 401

    def test_wrong_token_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/system",
            headers={"Authorization": "Bearer wrongtoken"},
        )
        assert response.status_code == 401

    def test_malformed_auth_scheme_returns_401(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/system",
            headers={"Authorization": "Token sometoken"},
        )
        assert response.status_code == 401

    def test_valid_token_returns_200(self, client: TestClient, auth_token: str) -> None:
        response = client.get(
            "/api/v1/system",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 200

    def test_wwwauthenticate_header_present_on_401(self, client: TestClient) -> None:
        response = client.get("/api/v1/system")
        assert "www-authenticate" in {k.lower() for k in response.headers}

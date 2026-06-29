"""Unit tests for password hashing, verification, and rehash detection."""

from __future__ import annotations

from timelapse_manager.config.settings import AuthSettings
from timelapse_manager.security.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)


def _fast_settings() -> AuthSettings:
    """Return AuthSettings with minimal Argon2 cost so tests are fast."""
    return AuthSettings(
        argon2_memory_kib=256,
        argon2_time_cost=1,
        argon2_parallelism=1,
    )


class TestHashPassword:
    def test_returns_a_string(self) -> None:
        s = _fast_settings()
        result = hash_password("correct-horse-battery-staple", s)
        assert isinstance(result, str)

    def test_returns_argon2_format_string(self) -> None:
        s = _fast_settings()
        result = hash_password("my-secure-password-123", s)
        assert result.startswith("$argon2")

    def test_two_calls_with_same_password_produce_different_hashes(self) -> None:
        # Different salts each time.
        s = _fast_settings()
        h1 = hash_password("same-password-1234", s)
        h2 = hash_password("same-password-1234", s)
        assert h1 != h2

    def test_empty_string_is_hashable(self) -> None:
        s = _fast_settings()
        result = hash_password("", s)
        assert result.startswith("$argon2")


class TestVerifyPassword:
    def test_correct_password_returns_true(self) -> None:
        s = _fast_settings()
        hashed = hash_password("hunter2-extra-secure!", s)
        assert verify_password("hunter2-extra-secure!", hashed, s) is True

    def test_wrong_password_returns_false(self) -> None:
        s = _fast_settings()
        hashed = hash_password("correct-password-long!", s)
        assert verify_password("wrong-password-long!!", hashed, s) is False

    def test_none_hash_never_verifies(self) -> None:
        # Sentinel and not-yet-provisioned accounts have a NULL hash.
        s = _fast_settings()
        assert verify_password("any-password-here!", None, s) is False

    def test_empty_string_hash_never_verifies(self) -> None:
        s = _fast_settings()
        assert verify_password("any-password-here!", "", s) is False

    def test_corrupted_hash_never_verifies(self) -> None:
        s = _fast_settings()
        assert verify_password("any-password-here!", "not-a-hash-at-all", s) is False

    def test_truncated_hash_never_verifies(self) -> None:
        s = _fast_settings()
        hashed = hash_password("correct-password-abc!", s)
        truncated = hashed[:10]
        assert verify_password("correct-password-abc!", truncated, s) is False

    def test_empty_password_against_real_hash_is_false(self) -> None:
        s = _fast_settings()
        hashed = hash_password("non-empty-password-!", s)
        assert verify_password("", hashed, s) is False

    def test_verify_is_case_sensitive(self) -> None:
        s = _fast_settings()
        hashed = hash_password("Password123Secure!", s)
        assert verify_password("password123secure!", hashed, s) is False


class TestNeedsRehash:
    def test_hash_at_current_settings_does_not_need_rehash(self) -> None:
        s = _fast_settings()
        hashed = hash_password("password-to-hash-1!", s)
        assert needs_rehash(hashed, s) is False

    def test_hash_at_weaker_settings_needs_rehash(self) -> None:
        # Hash with very low cost, check with higher cost.
        weak = AuthSettings(
            argon2_memory_kib=8, argon2_time_cost=1, argon2_parallelism=1
        )
        strong = AuthSettings(
            argon2_memory_kib=256, argon2_time_cost=2, argon2_parallelism=1
        )
        hashed = hash_password("password-rehash-test!", weak)
        assert needs_rehash(hashed, strong) is True

    def test_corrupted_hash_needs_rehash(self) -> None:
        s = _fast_settings()
        assert needs_rehash("this-is-not-a-valid-hash", s) is True

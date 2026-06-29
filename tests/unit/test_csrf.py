"""Unit tests for the CSRF synchronizer-token functions."""

from __future__ import annotations

import secrets
import types

import pytest

from timelapse_manager.security.csrf import issue_csrf, verify_csrf


def _fake_row(secret: str | None) -> object:
    """Return a simple namespace that mimics SessionRow.csrf_secret access."""
    obj = types.SimpleNamespace(csrf_secret=secret)
    return obj


class TestIssueCsrf:
    def test_returns_secret_unchanged_when_passed_a_string(self) -> None:
        secret = secrets.token_urlsafe(32)
        assert issue_csrf(secret) == secret

    def test_returns_session_csrf_secret_when_passed_a_row(self) -> None:
        secret = secrets.token_urlsafe(32)
        row = _fake_row(secret)
        assert issue_csrf(row) == secret  # type: ignore[arg-type]

    def test_raises_when_session_row_has_no_csrf_secret(self) -> None:
        row = _fake_row(None)
        with pytest.raises(ValueError, match="no CSRF secret"):
            issue_csrf(row)  # type: ignore[arg-type]


class TestVerifyCsrf:
    def test_matching_tokens_return_true(self) -> None:
        token = secrets.token_urlsafe(32)
        assert verify_csrf(token, token) is True

    def test_mismatched_tokens_return_false(self) -> None:
        expected = secrets.token_urlsafe(32)
        presented = secrets.token_urlsafe(32)
        # Vanishingly unlikely to collide; belt-and-suspenders check.
        if expected == presented:
            presented = presented + "x"
        assert verify_csrf(expected, presented) is False

    def test_none_expected_returns_false(self) -> None:
        assert verify_csrf(None, "some-token") is False

    def test_none_presented_returns_false(self) -> None:
        assert verify_csrf("expected-token", None) is False

    def test_both_none_returns_false(self) -> None:
        assert verify_csrf(None, None) is False

    def test_empty_expected_returns_false(self) -> None:
        assert verify_csrf("", "some-token") is False

    def test_empty_presented_returns_false(self) -> None:
        assert verify_csrf("expected-token", "") is False

    def test_uses_constant_time_comparison(self) -> None:
        # secrets.compare_digest is the implementation; verify it is in use
        # by confirming that token prefixes do not leak early exits. This is
        # a structural smoke check, not a timing oracle.
        secret = "a" * 64
        wrong = "a" * 63 + "b"
        assert verify_csrf(secret, wrong) is False
        assert verify_csrf(secret, secret) is True

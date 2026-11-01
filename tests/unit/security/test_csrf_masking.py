"""Masked CSRF tokens and the fetch-metadata check (#70 F-S4, WP-70-G)."""

from __future__ import annotations

import base64
import secrets

import pytest

from timelapse_manager.security.csrf import (
    fetch_site_rejected,
    issue_csrf,
    mask_secret,
    verify_csrf,
)


def _secret() -> str:
    # The shape sessions.create_session mints.
    return secrets.token_urlsafe(32)


class TestMasking:
    def test_two_issues_differ_and_both_verify(self) -> None:
        secret = _secret()
        first, second = issue_csrf(secret), issue_csrf(secret)
        assert first != second
        assert verify_csrf(secret, first) is True
        assert verify_csrf(secret, second) is True

    def test_masked_token_has_a_fixed_length_and_alphabet(self) -> None:
        # A length that varied with the secret's bytes would itself be a leak.
        secret = _secret()
        lengths = {len(issue_csrf(secret)) for _ in range(50)}
        assert len(lengths) == 1
        token = issue_csrf(secret)
        assert set(token) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )

    def test_masked_token_does_not_contain_the_secret(self) -> None:
        secret = _secret()
        for _ in range(20):
            token = issue_csrf(secret)
            assert secret not in token
            assert secret.encode() not in base64.urlsafe_b64decode(
                token + "=" * (-len(token) % 4)
            )

    def test_raw_secret_is_refused(self) -> None:
        # The compatibility path for pre-masking tabs is gone (#188).
        secret = _secret()
        assert verify_csrf(secret, secret) is False
        assert verify_csrf(secret, issue_csrf(secret)) is True

    def test_token_from_another_session_fails(self) -> None:
        mine, theirs = _secret(), _secret()
        assert verify_csrf(mine, issue_csrf(theirs)) is False
        assert verify_csrf(mine, theirs) is False

    def test_tampered_masked_token_fails(self) -> None:
        secret = _secret()
        token = issue_csrf(secret)
        flipped = token[:10] + ("A" if token[10] != "A" else "B") + token[11:]
        assert verify_csrf(secret, flipped) is False

    @pytest.mark.parametrize(
        "presented",
        [
            "",
            "!!!!",
            "not base64 at all",
            # 115 characters is the masked length for a 43-character secret,
            # so these reach the unmask path rather than the bare compare.
            "\u00e9" * 115,
            "\ud800" * 115,
            "=" * 115,
            "A" * 115,
            "A" * 113 + "==",
        ],
    )
    def test_malformed_values_fail_without_raising(self, presented: str) -> None:
        assert verify_csrf(_secret(), presented) is False

    def test_wrong_length_fails(self) -> None:
        secret = _secret()
        token = issue_csrf(secret)
        assert verify_csrf(secret, token[:-1]) is False
        assert verify_csrf(secret, token + "A") is False

    def test_padded_or_standard_alphabet_forms_are_not_accepted(self) -> None:
        # Exactly one spelling is a masked token: no padding, no "+" or "/".
        secret = _secret()
        token = issue_csrf(secret)
        assert verify_csrf(secret, token + "=" * (-len(token) % 4 or 4)) is False
        standard = token.replace("-", "+").replace("_", "/")
        if standard != token:
            assert verify_csrf(secret, standard) is False

    def test_masked_token_of_another_secret_length_fails(self) -> None:
        secret = _secret()
        assert verify_csrf(secret, mask_secret(secret + "x")) is False

    def test_empty_secret_never_matches(self) -> None:
        assert verify_csrf("", issue_csrf("")) is False
        assert verify_csrf(None, issue_csrf(_secret())) is False


class TestFetchSiteRejected:
    @pytest.mark.parametrize("value", ["cross-site", "same-site", " Cross-Site "])
    def test_other_sites_are_rejected(self, value: str) -> None:
        assert fetch_site_rejected({"sec-fetch-site": value}) is True

    @pytest.mark.parametrize("value", ["same-origin", "none", "", "unknown-value"])
    def test_same_origin_and_unknown_fall_through(self, value: str) -> None:
        assert fetch_site_rejected({"sec-fetch-site": value}) is False

    def test_missing_header_falls_through(self) -> None:
        assert fetch_site_rejected({}) is False

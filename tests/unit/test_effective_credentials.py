"""Unit tests for the effective-credential resolution seam.

``effective_credentials`` is the pure decision the adapter factory makes about
which login to use: a camera's own credentials always win; a camera with none of
its own falls back to the global default only when it is configured to inherit it
and a default is available; otherwise the camera stays open (None). No I/O.
"""

from __future__ import annotations

from types import SimpleNamespace

from timelapse_manager.cameras.registry import effective_credentials

_DEFAULT = ("default-user", "default-pass")
_OWN_DOC = {"username": "own-user", "password": "own-pass"}
_OWN_PAIR = ("own-user", "own-pass")


def _camera(*, credentials: dict[str, object] | None, inherit: bool) -> SimpleNamespace:
    """Build a minimal camera double exposing the two fields the seam reads."""
    return SimpleNamespace(credentials=credentials, credentials_inherit_default=inherit)


class TestEffectiveCredentials:
    def test_own_credentials_win_over_default(self) -> None:
        cam = _camera(credentials=_OWN_DOC, inherit=True)
        assert effective_credentials(cam, _DEFAULT) == _OWN_PAIR

    def test_own_credentials_used_even_when_flag_off(self) -> None:
        cam = _camera(credentials=_OWN_DOC, inherit=False)
        assert effective_credentials(cam, _DEFAULT) == _OWN_PAIR

    def test_inherit_with_default_uses_default(self) -> None:
        cam = _camera(credentials=None, inherit=True)
        assert effective_credentials(cam, _DEFAULT) == _DEFAULT

    def test_inherit_without_default_is_none(self) -> None:
        cam = _camera(credentials=None, inherit=True)
        assert effective_credentials(cam, None) is None

    def test_flag_off_no_own_is_none(self) -> None:
        cam = _camera(credentials=None, inherit=False)
        assert effective_credentials(cam, _DEFAULT) is None

    def test_missing_inherit_attribute_defaults_off(self) -> None:
        # A lightweight double without the new field must not error and must not
        # inherit (the seam reads it via getattr with a False default).
        cam = SimpleNamespace(credentials=None)
        assert effective_credentials(cam, _DEFAULT) is None

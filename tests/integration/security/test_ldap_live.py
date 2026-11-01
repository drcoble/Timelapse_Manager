"""Live integration tests for LDAP authentication against a real FreeIPA directory.

These tests talk to a real production-grade directory server over LDAPS with CA
validation.  They are gated behind the ``ldap_live`` marker and a mandatory env-var
check: when any required variable is absent the entire module skips cleanly with a
single, clear message.  A plain ``pytest`` run (no ``-m ldap_live``) never executes
these tests.

Run with::

    set -a && . /etc/timelapse-test/ldap.env && set +a
    uv run pytest -m ldap_live -q

CA-cert note
-----------
``_build_server_pool`` builds the ``ldap3.Tls`` object with
``validate=ssl.CERT_REQUIRED`` (no skip-verification option) and an optional
``ca_certs_file`` taken from ``LdapSettingsView.tls_ca_cert_path``.  Two trust
sources are exercised end to end:

* ``live_ldaps_view`` (the legacy/back-compat path) leaves ``tls_ca_cert_path``
  unset and injects the CA via the ``SSL_CERT_FILE`` env var, so validation falls
  back to OpenSSL's default verify paths -- how a deployment trusts a private CA
  without using the field.
* ``TestTlsCaCertPath`` exercises the field itself: the configured PEM path is the
  trust anchor, with negative controls proving the path is actually consulted and
  that the private CA is NOT trusted by default on this host.

Account-lockout guard
---------------------
The target is a production directory with a real Kerberos lockout counter.  ONLY
the service-account bind (``TLM_TEST_LDAP_BIND_DN`` / ``TLM_TEST_LDAP_BIND_PW``) is
used in all tests.  No user rebind with a wrong password is ever attempted.
The nonexistent-username test uses ``resolve_directory_state``, which never
performs a user rebind at all (line 464 in ldap_directory.py short-circuits before
any rebind).  The conditional full-credential test (case 4) runs ONLY when
``TLM_TEST_LDAP_USER_PW`` is set, and only submits the correct password.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

import pytest

from timelapse_manager.db.session import session_scope
from timelapse_manager.security.ldap_directory import (
    LdapOutcome,
    authenticate,
    map_groups_to_role,
    normalize_dn,
    provision_user,
    resolve_directory_state,
)
from timelapse_manager.security.ldap_settings_service import LdapSettingsView

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Required env-var names (all values read at fixture time, never at import).
# ---------------------------------------------------------------------------

_E_URL_TLS = "TLM_TEST_LDAP_URL_TLS"
_E_CACERT = "TLM_TEST_LDAP_CACERT"
_E_BASE_DN = "TLM_TEST_LDAP_BASE_DN"
_E_USER_BASE = "TLM_TEST_LDAP_USER_BASE"
_E_GROUP_BASE = "TLM_TEST_LDAP_GROUP_BASE"
_E_USERNAME_ATTR = "TLM_TEST_LDAP_USERNAME_ATTR"
_E_BIND_DN = "TLM_TEST_LDAP_BIND_DN"
_E_BIND_PW = "TLM_TEST_LDAP_BIND_PW"
_E_USER = "TLM_TEST_LDAP_USER"
_E_ROLE_GROUP_DN = "TLM_TEST_LDAP_ROLE_GROUP_DN"
_E_ROLE_GROUP_ROLE = "TLM_TEST_LDAP_ROLE_GROUP_ROLE"
_E_USER_PW = "TLM_TEST_LDAP_USER_PW"  # MAY be unset -- skips case 4

_REQUIRED_VARS = (
    _E_URL_TLS,
    _E_CACERT,
    _E_BASE_DN,
    _E_USER_BASE,
    _E_USERNAME_ATTR,
    _E_BIND_DN,
    _E_BIND_PW,
    _E_USER,
    _E_ROLE_GROUP_DN,
    _E_ROLE_GROUP_ROLE,
)

# All tests in this module require the ldap_live marker.
pytestmark = pytest.mark.ldap_live


# ---------------------------------------------------------------------------
# Module-scope skip guard: one check gates the entire module.
# ---------------------------------------------------------------------------


def _require_env_vars() -> dict[str, str]:
    """Return a dict of required env vars, or skip if any are missing."""
    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        pytest.skip(
            f"ldap_live tests require env vars (source /etc/timelapse-test/ldap.env)."
            f" Missing: {', '.join(missing)}",
            allow_module_level=True,
        )
    return {v: os.environ[v] for v in _REQUIRED_VARS}


# Evaluated once at collection time; skips the module if vars are absent.
_env = _require_env_vars()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def env() -> dict[str, str]:
    """Return the required env vars as a dict (already validated at module load)."""
    return _env


@pytest.fixture()
def live_ldaps_view(
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> LdapSettingsView:
    """Return a LdapSettingsView wired to the live directory over LDAPS with CA.

    This fixture deliberately leaves ``tls_ca_cert_path`` unset and trusts the
    private CA via the ``SSL_CERT_FILE`` env var, so the connector's
    ``ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=None)`` falls back to
    OpenSSL's default verify paths -- the back-compat path that needs no code
    change.  The ``tls_ca_cert_path`` field itself is exercised separately in
    ``TestTlsCaCertPath``, which builds its own per-case views.
    """
    ca_path = env[_E_CACERT]
    if not os.path.exists(ca_path):
        pytest.skip(
            f"CA cert file not found at {ca_path!r} (from {_E_CACERT}); "
            "cannot verify LDAPS without it"
        )
    monkeypatch.setenv("SSL_CERT_FILE", ca_path)

    return _build_view(env, tls_ca_cert_path=None)


def _build_view(
    env: dict[str, str], *, tls_ca_cert_path: str | None
) -> LdapSettingsView:
    """Build an LDAPS settings view for the live directory from env.

    ``tls_ca_cert_path`` is passed through verbatim so a caller can exercise the
    field directly (real CA, wrong CA, or ``None`` for the SSL_CERT_FILE/OS-store
    fallback).  Group mapping maps the configured role group DN to its role.
    """
    role = env[_E_ROLE_GROUP_ROLE].lower()
    viewer_dn = env[_E_ROLE_GROUP_DN] if role == "viewer" else ""
    operator_dn = env[_E_ROLE_GROUP_DN] if role == "operator" else ""
    admin_dn = env[_E_ROLE_GROUP_DN] if role == "admin" else ""

    return LdapSettingsView(
        enabled=True,
        server_urls=[env[_E_URL_TLS]],
        tls_mode="ldaps",
        tls_ca_cert_path=tls_ca_cert_path,
        bind_dn=env[_E_BIND_DN],
        bind_password="***",  # mask sentinel; real password passed to authenticate()
        bind_password_set=True,
        search_base=env[_E_USER_BASE],
        search_filter="",
        group_search_base=env.get(_E_GROUP_BASE, ""),
        username_attribute=env[_E_USERNAME_ATTR],
        display_name_attribute="",
        membership_mode="memberof",
        nested_groups=False,
        admin_group_dn=admin_dn,
        operator_group_dn=operator_dn,
        viewer_group_dn=viewer_dn,
    )


# ---------------------------------------------------------------------------
# Case 1: resolve_directory_state over LDAPS
# ---------------------------------------------------------------------------


class TestResolveDirectoryState:
    """Verify password-less user lookup and group resolution over LDAPS."""

    def test_resolve_finds_configured_user_and_role_group(
        self,
        live_ldaps_view: LdapSettingsView,
        env: dict[str, str],
    ) -> None:
        """resolve_directory_state finds the test user and their role group.

        Uses the service-account bind only (no user rebind).  Asserts:
        - outcome is AUTHENTICATED (account found)
        - the configured role group DN appears in the resolved groups
        - map_groups_to_role returns the configured role
        """
        username = env[_E_USER]
        bind_pw = env[_E_BIND_PW]
        role_group_dn = env[_E_ROLE_GROUP_DN]
        expected_role = env[_E_ROLE_GROUP_ROLE].lower()

        state = resolve_directory_state(
            settings=live_ldaps_view,
            username=username,
            bind_password=bind_pw,
        )

        assert state.outcome is LdapOutcome.AUTHENTICATED, (
            f"Expected AUTHENTICATED for user {username!r}; got {state.outcome}"
            f" (detail: {state.detail!r})"
        )
        assert state.found is True

        # DN comparison must be case/whitespace-insensitive (normalize_dn mirrors
        # what _build_server_pool and map_groups_to_role do internally).
        normalized_role_dn = normalize_dn(role_group_dn)
        normalized_groups = {normalize_dn(g) for g in state.groups}
        assert normalized_role_dn in normalized_groups, (
            f"Role group DN {role_group_dn!r} not found in resolved groups.\n"
            f"Got (normalized): {normalized_groups}"
        )

        role = map_groups_to_role(
            state.groups,
            admin_group_dn=live_ldaps_view.admin_group_dn or None,
            operator_group_dn=live_ldaps_view.operator_group_dn or None,
            viewer_group_dn=live_ldaps_view.viewer_group_dn or None,
        )
        assert role == expected_role, (
            f"Expected role {expected_role!r}; map_groups_to_role returned {role!r}.\n"
            f"Groups from directory: {state.groups}"
        )


# ---------------------------------------------------------------------------
# Case 2: provision_user in a temp DB -- create then idempotent second call
# ---------------------------------------------------------------------------


class TestProvisionUser:
    """Verify JIT provisioning into a real migrated temp DB."""

    def test_provision_creates_ldap_user_and_second_call_is_idempotent(
        self,
        live_ldaps_view: LdapSettingsView,
        env: dict[str, str],
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        """provision_user creates an ldap user row; a second call updates in place.

        First call: creates User with auth_source='ldap', the mapped role, no
        password hash, and enabled=True.
        Second call: the same row is returned with no duplicate created.
        """
        username = env[_E_USER]
        bind_pw = env[_E_BIND_PW]
        expected_role = env[_E_ROLE_GROUP_ROLE].lower()

        # Resolve the role from the live directory first.
        state = resolve_directory_state(
            settings=live_ldaps_view,
            username=username,
            bind_password=bind_pw,
        )
        assert state.outcome is LdapOutcome.AUTHENTICATED, (
            f"resolve_directory_state prerequisite failed: {state.outcome}"
        )
        role = map_groups_to_role(
            state.groups,
            admin_group_dn=live_ldaps_view.admin_group_dn or None,
            operator_group_dn=live_ldaps_view.operator_group_dn or None,
            viewer_group_dn=live_ldaps_view.viewer_group_dn or None,
        )
        assert role == expected_role, (
            f"Role mapping prerequisite failed:"
            f" expected {expected_role!r}, got {role!r}"
        )

        # First call: creates the row.
        with session_scope(migrated_factory) as db:
            user = provision_user(
                db,
                username=username,
                role=role,
                display_name=username,
            )
            assert user.auth_source == "ldap"
            assert user.role == role
            assert user.password_hash is None
            assert user.enabled is True
            user_id = user.id

        # Second call: must not create a duplicate -- same ID returned.
        with session_scope(migrated_factory) as db:
            user2 = provision_user(
                db,
                username=username,
                role=role,
                display_name=username,
            )
            assert user2.id == user_id, (
                "Second provision_user call must reuse the existing"
                " row, not create a duplicate"
            )
            assert user2.auth_source == "ldap"

        # Verify exactly one row exists in the DB.
        from timelapse_manager.db.models import User

        with session_scope(migrated_factory) as db:
            count = db.query(User).filter(User.username == username).count()
        assert count == 1, (
            f"Expected exactly 1 User row for {username!r}; found {count}"
        )


# ---------------------------------------------------------------------------
# Case 3: NO_SUCH_USER with a random nonexistent username
# ---------------------------------------------------------------------------


class TestNoSuchUser:
    """Verify the NO_SUCH_USER path without any user rebind."""

    def test_resolve_nonexistent_username_returns_no_such_user(
        self,
        live_ldaps_view: LdapSettingsView,
        env: dict[str, str],
    ) -> None:
        """A random UUID username that cannot exist returns NO_SUCH_USER.

        Uses resolve_directory_state (not authenticate) so no user rebind is
        ever attempted -- the service-account bind finds no entry and returns
        early without touching any lockout counter.
        """
        bind_pw = env[_E_BIND_PW]
        # uuid4 hex is 32 hex chars; guaranteed not a real username.
        ghost = f"ghost-{uuid.uuid4().hex}"

        state = resolve_directory_state(
            settings=live_ldaps_view,
            username=ghost,
            bind_password=bind_pw,
        )

        assert state.outcome is LdapOutcome.NO_SUCH_USER, (
            f"Expected NO_SUCH_USER for {ghost!r}; got {state.outcome}"
            f" (detail: {state.detail!r})"
        )
        assert state.found is False
        assert state.groups == frozenset()


# ---------------------------------------------------------------------------
# Case 4: Full credential bind -- CONDITIONAL on TLM_TEST_LDAP_USER_PW
# ---------------------------------------------------------------------------


class TestFullCredentialBind:
    """Verify the full authenticate() path including user rebind (correct pw only)."""

    def test_authenticate_with_correct_user_password(
        self,
        live_ldaps_view: LdapSettingsView,
        env: dict[str, str],
    ) -> None:
        """authenticate() returns AUTHENTICATED with the correct user password.

        Skipped when TLM_TEST_LDAP_USER_PW is unset.  Only the correct password
        is ever submitted -- this test NEVER tests wrong credentials to avoid
        incrementing the production lockout counter.
        """
        user_pw = os.environ.get(_E_USER_PW)
        if not user_pw:
            pytest.skip(f"{_E_USER_PW} is not set; skipping full credential bind test")

        username = env[_E_USER]
        bind_pw = env[_E_BIND_PW]
        expected_role = env[_E_ROLE_GROUP_ROLE].lower()

        result = authenticate(
            settings=live_ldaps_view,
            username=username,
            password=user_pw,
            bind_password=bind_pw,
        )

        assert result.outcome is LdapOutcome.AUTHENTICATED, (
            f"Expected AUTHENTICATED for user {username!r}; got {result.outcome}"
            f" (detail: {result.detail!r})"
        )
        assert result.authenticated is True
        assert result.dn != "", "Authenticated result must have a non-empty DN"

        # The configured role group DN must appear in the resolved groups.
        role_group_dn = env[_E_ROLE_GROUP_DN]
        normalized_role_dn = normalize_dn(role_group_dn)
        normalized_groups = {normalize_dn(g) for g in result.groups}
        assert normalized_role_dn in normalized_groups, (
            f"Role group DN {role_group_dn!r} not in groups after authenticate.\n"
            f"Got (normalized): {normalized_groups}"
        )

        role = map_groups_to_role(
            result.groups,
            admin_group_dn=live_ldaps_view.admin_group_dn or None,
            operator_group_dn=live_ldaps_view.operator_group_dn or None,
            viewer_group_dn=live_ldaps_view.viewer_group_dn or None,
        )
        assert role == expected_role, (
            f"Expected role {expected_role!r}; map_groups_to_role returned {role!r}"
        )


# ---------------------------------------------------------------------------
# Case 5: the tls_ca_cert_path setting is actually consulted (private-CA trust)
# ---------------------------------------------------------------------------


def _generate_unrelated_ca(tmp_path) -> str:  # type: ignore[no-untyped-def]
    """Write an ephemeral, self-signed CA PEM (unrelated to the directory).

    A real, well-formed CA certificate that passes ldap3's Tls construction but
    has NOT signed the directory's server cert, so pointing the connector at it
    must make TLS verification fail.  Proves the configured file is consulted.
    """
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tlm-unrelated-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem_path = tmp_path / "unrelated-ca.pem"
    pem_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(pem_path)


class TestTlsCaCertPath:
    """Prove the ``tls_ca_cert_path`` setting is actually consulted for trust.

    These build their own per-case views (NOT the ``live_ldaps_view`` fixture,
    which force-sets ``SSL_CERT_FILE``) so the discriminator is honest.  All cases
    use the HOSTNAME LDAPS URL (``TLM_TEST_LDAP_URL_TLS``) and the service-account
    bind only via ``resolve_directory_state`` -- never a real-user bind -- so the
    negative cases fail at the TLS handshake before any bind and carry ZERO
    Kerberos-lockout risk.

    The acceptance proof is the **A-vs-C differential**: A (real CA file) and C
    (an unrelated CA file) are identical in every input -- same host, ``SSL_CERT_FILE``
    removed in both -- and differ ONLY in the CA file content.  A succeeds and C
    fails verification, so A's success is attributable solely to ``tls_ca_cert_path``.
    This holds even where the host default-trusts the private CA (a wrong CA file
    REPLACES the system store rather than unioning with it, so C still fails).  Case
    B is the cleaner negative control on a host that does not default-trust the CA;
    where it does, B skips loudly and defers to C.  Case D proves the unset ->
    ``SSL_CERT_FILE`` / OS-store fallback still works.
    """

    def test_a_positive_configured_ca_path_succeeds(
        self,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A: tls_ca_cert_path = the real CA -> resolve SUCCEEDS (user found)."""
        ca_path = env[_E_CACERT]
        if not os.path.exists(ca_path):
            pytest.skip(f"CA cert file not found at {ca_path!r} (from {_E_CACERT})")
        # Remove any ambient trust so success can only come from the configured
        # path, not an OS-store / SSL_CERT_FILE fallback.
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)

        view = _build_view(env, tls_ca_cert_path=ca_path)
        state = resolve_directory_state(
            settings=view,
            username=env[_E_USER],
            bind_password=env[_E_BIND_PW],
        )
        assert state.outcome is LdapOutcome.AUTHENTICATED, (
            f"Case A: configured CA path should validate and find the user; "
            f"got {state.outcome} (detail: {state.detail!r})"
        )
        assert state.found is True
        role = map_groups_to_role(
            state.groups,
            admin_group_dn=view.admin_group_dn or None,
            operator_group_dn=view.operator_group_dn or None,
            viewer_group_dn=view.viewer_group_dn or None,
        )
        assert role == env[_E_ROLE_GROUP_ROLE].lower()

    def test_b_negative_no_ca_no_ssl_cert_file_fails_by_default(
        self,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """B (negative control): no CA path AND no SSL_CERT_FILE -> MUST FAIL TLS.

        On a host whose default trust store does NOT already contain the directory's
        private CA, this is the hard negative control: validation must fail, proving
        the private CA is not trusted by default.

        On a host that DOES default-trust the private CA (e.g. one enrolled as a
        FreeIPA client, which installs the IPA CA system-wide), this control cannot
        hold -- so we ``pytest.skip`` with a pointed reason rather than failing or
        silently passing. This is NOT a silent skip: it states loudly that B is
        inapplicable here and that ``test_c_wrong_ca_fails_verification`` carries the
        proof instead. Case C is independent of ambient trust: it sets a wrong
        ``ca_certs_file`` and still fails verification, which (paired with case A's
        success under identical ambient trust) proves the field alone determines the
        outcome.
        """
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)

        view = _build_view(env, tls_ca_cert_path=None)
        state = resolve_directory_state(
            settings=view,
            username=env[_E_USER],
            bind_password=env[_E_BIND_PW],
        )
        if state.outcome is LdapOutcome.AUTHENTICATED:
            pytest.skip(
                "Host default trust store already contains the directory's private "
                "CA (likely FreeIPA client enrollment): with no configured CA path "
                "and no SSL_CERT_FILE the connector still validated. Case B cannot "
                "serve as the negative control on this host -- trust is proven by "
                "case C (wrong-CA replaces the system store and fails verification) "
                "paired with case A (real CA succeeds under identical ambient trust)."
            )
        assert state.outcome in (
            LdapOutcome.SERVER_UNREACHABLE,
            LdapOutcome.CONFIG_ERROR,
        ), (
            "Case B (negative control): with no configured CA path and no "
            "SSL_CERT_FILE, the private CA must NOT be trusted by default, so TLS "
            f"validation must fail. Instead got {state.outcome} "
            f"(detail: {state.detail!r})."
        )

    def test_c_wrong_ca_fails_verification(
        self,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,  # type: ignore[no-untyped-def]
    ) -> None:
        """C: tls_ca_cert_path = an unrelated self-signed CA -> MUST FAIL verify.

        Proves the configured file is actually consulted: a well-formed CA that did
        not sign the server cert cannot validate it.
        """
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        wrong_ca = _generate_unrelated_ca(tmp_path)

        view = _build_view(env, tls_ca_cert_path=wrong_ca)
        state = resolve_directory_state(
            settings=view,
            username=env[_E_USER],
            bind_password=env[_E_BIND_PW],
        )
        assert state.outcome in (
            LdapOutcome.SERVER_UNREACHABLE,
            LdapOutcome.CONFIG_ERROR,
        ), (
            "Case C: an unrelated CA must fail TLS verification (proving the "
            f"configured path is consulted); got {state.outcome} "
            f"(detail: {state.detail!r})"
        )

    def test_d_fallback_ssl_cert_file_still_works(
        self,
        env: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D (back-compat): no CA path BUT SSL_CERT_FILE set -> SUCCEEDS.

        Proves the unset -> OS-store / SSL_CERT_FILE fallback still works, so
        private-CA trust without the field (via the system trust store) is preserved.
        """
        ca_path = env[_E_CACERT]
        if not os.path.exists(ca_path):
            pytest.skip(f"CA cert file not found at {ca_path!r} (from {_E_CACERT})")
        monkeypatch.setenv("SSL_CERT_FILE", ca_path)

        view = _build_view(env, tls_ca_cert_path=None)
        state = resolve_directory_state(
            settings=view,
            username=env[_E_USER],
            bind_password=env[_E_BIND_PW],
        )
        assert state.outcome is LdapOutcome.AUTHENTICATED, (
            "Case D: with SSL_CERT_FILE pointed at the CA and no configured path, "
            f"the fallback must still validate; got {state.outcome} "
            f"(detail: {state.detail!r})"
        )
        assert state.found is True

"""Unit tests for the pure group->role mapping function.

Exhaustive over the precedence rule (highest privilege wins), each role in
isolation, multi-group membership, case-insensitive DN matching, and the
no-match-no-role contract that gates JIT provisioning.
"""

from __future__ import annotations

import pytest

from timelapse_manager.security.ldap_directory import (
    _directory_suffix,
    map_groups_to_role,
    normalize_dn,
)

_ADMIN = "cn=tl-admins,ou=groups,dc=example,dc=com"
_OPERATOR = "cn=tl-operators,ou=groups,dc=example,dc=com"
_VIEWER = "cn=tl-viewers,ou=groups,dc=example,dc=com"

_GROUP_CONFIG = {
    "admin_group_dn": _ADMIN,
    "operator_group_dn": _OPERATOR,
    "viewer_group_dn": _VIEWER,
}


class TestSingleRoleMapping:
    def test_admin_group_maps_to_admin(self) -> None:
        assert map_groups_to_role(frozenset({_ADMIN}), **_GROUP_CONFIG) == "admin"

    def test_operator_group_maps_to_operator(self) -> None:
        role = map_groups_to_role(frozenset({_OPERATOR}), **_GROUP_CONFIG)
        assert role == "operator"

    def test_viewer_group_maps_to_viewer(self) -> None:
        assert map_groups_to_role(frozenset({_VIEWER}), **_GROUP_CONFIG) == "viewer"


class TestHighestPrivilegeWins:
    def test_admin_and_viewer_yields_admin(self) -> None:
        role = map_groups_to_role(frozenset({_VIEWER, _ADMIN}), **_GROUP_CONFIG)
        assert role == "admin"

    def test_operator_and_viewer_yields_operator(self) -> None:
        role = map_groups_to_role(frozenset({_VIEWER, _OPERATOR}), **_GROUP_CONFIG)
        assert role == "operator"

    def test_all_three_yields_admin(self) -> None:
        role = map_groups_to_role(
            frozenset({_VIEWER, _OPERATOR, _ADMIN}), **_GROUP_CONFIG
        )
        assert role == "admin"


class TestNoMatchYieldsNoRole:
    def test_unmapped_group_yields_none(self) -> None:
        role = map_groups_to_role(
            frozenset({"cn=other,ou=groups,dc=example,dc=com"}), **_GROUP_CONFIG
        )
        assert role is None

    def test_empty_membership_yields_none(self) -> None:
        assert map_groups_to_role(frozenset(), **_GROUP_CONFIG) is None

    def test_unconfigured_groups_yield_none(self) -> None:
        # No role group DNs configured at all -> nobody is granted a role.
        role = map_groups_to_role(
            frozenset({_ADMIN}),
            admin_group_dn=None,
            operator_group_dn=None,
            viewer_group_dn=None,
        )
        assert role is None


class TestCaseInsensitiveDnMatching:
    def test_uppercase_member_dn_matches_lowercase_config(self) -> None:
        upper = _ADMIN.upper()
        assert map_groups_to_role(frozenset({upper}), **_GROUP_CONFIG) == "admin"

    def test_whitespace_around_rdn_separators_is_tolerated(self) -> None:
        spaced = "cn=tl-admins, ou=groups, dc=example, dc=com"
        assert map_groups_to_role(frozenset({spaced}), **_GROUP_CONFIG) == "admin"


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("CN=X,DC=Example,DC=COM", "cn=x,dc=example,dc=com"),
        ("cn=x, dc=example, dc=com", "cn=x,dc=example,dc=com"),
    ],
)
def test_normalize_dn_canonicalises_equivalent_dns(a: str, b: str) -> None:
    assert normalize_dn(a) == normalize_dn(b)


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        # The user OU is stripped down to the naming-context root so a group
        # search reaches a sibling ou=groups subtree.
        ("ou=people,dc=example,dc=com", "dc=example,dc=com"),
        ("cn=alice,ou=staff,ou=people,dc=example,dc=com", "dc=example,dc=com"),
        ("dc=example,dc=com", "dc=example,dc=com"),
        # Spacing around separators is tolerated.
        ("ou=people, dc=example, dc=com", "dc=example,dc=com"),
        # No dc components -> returned unchanged (best effort).
        ("o=Example,c=US", "o=Example,c=US"),
    ],
)
def test_directory_suffix_extracts_naming_context(base: str, expected: str) -> None:
    assert _directory_suffix(base) == expected

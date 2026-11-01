"""Username normalisation: one person, one account, whatever case they type.

Directory servers match login names case-insensitively, and people type their
names in whatever case they like, so the application compares usernames by a
*key*: surrounding whitespace removed, Unicode NFKC-normalised and casefolded.
The key is stored on every ``user`` row (``user.username_key``, unique) and all
lookups go through it (issue #4). The displayed ``username`` keeps a spelling:
the directory's own value for LDAP accounts, the stripped input for local ones.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence


def normalize_username(value: str) -> str:
    """Return ``value`` as it should be stored: surrounding whitespace removed."""
    return value.strip()


def username_key(value: str) -> str:
    """Return the comparison key for ``value`` (stripped, NFKC, casefolded)."""
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def directory_username(
    attributes: Mapping[str, Sequence[str]],
    username_attribute: str,
    fallback: str,
) -> str:
    """Return the directory's own spelling of the login name.

    Looks up ``username_attribute`` (attribute names are case-insensitive in
    LDAP) in the located entry's attributes and returns its first value; when it
    is missing or empty, returns the normalised ``fallback`` (the submitted name).
    """
    wanted = username_attribute.lower()
    for name, values in attributes.items():
        if name.lower() == wanted and values:
            canonical = normalize_username(str(values[0]))
            if canonical:
                return canonical
    return normalize_username(fallback)


__all__ = ["directory_username", "normalize_username", "username_key"]

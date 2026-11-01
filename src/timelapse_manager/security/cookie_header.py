"""Strict reading of the security cookies from the raw ``Cookie`` header.

``request.cookies`` is not used for a cookie whose name carries a prefix rule.
:func:`cookie_value` reads the header itself: pairs are split on ``;``, names
and values are trimmed of ASCII space and tab only, and the name is compared
exactly. It fails closed: a name that matches only after ``str.strip()`` (that
is, after Unicode whitespace trimming), or an expected name that appears more
than once, makes the cookie absent.
"""

from __future__ import annotations

from starlette.requests import Request


def cookie_lookup(request: Request, name: str) -> tuple[str | None, bool]:
    """Return ``(value, ambiguous)`` for cookie ``name``.

    ``ambiguous`` is True when the header holds a name that matches ``name``
    only after ``str.strip()``, or ``name`` more than once; the value is then
    ``None``. Callers that have a fallback name must treat ambiguity as
    "refuse", never as "absent": otherwise an inexact name would suppress the
    real cookie and steer the request to the fallback.
    """
    found: list[str] = []
    for header in request.headers.getlist("cookie"):
        for pair in header.split(";"):
            raw_name, separator, value = pair.partition("=")
            if not separator:
                continue
            if raw_name.strip(" \t") == name:
                found.append(value.strip(" \t"))
            elif raw_name.strip() == name:
                return None, True  # matches only after Unicode strip
    if len(found) > 1:
        return None, True
    return (found[0] if found else None), False


def cookie_value(request: Request, name: str) -> str | None:
    """Return the value of cookie ``name``, or ``None`` if absent or ambiguous."""
    return cookie_lookup(request, name)[0]

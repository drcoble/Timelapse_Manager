"""Transport-neutral errors raised by application services.

A service function validates its inputs and raises one of these when a write
cannot proceed. Each carries the HTTP status and a stable machine ``code`` a
transport layer maps to a response, plus a human ``detail`` and an optional
``field`` naming the offending input. Services stay ignorant of HTTP: they raise
these, and the web/API layers translate them (see the web error handler).
"""

from __future__ import annotations


class AppError(Exception):
    """Base for every service-layer error.

    :cvar status: the HTTP status a transport maps this error to.
    :cvar code: a stable machine-readable identifier for the error class,
        returned to JSON clients so they can branch on it without parsing prose.
    """

    status: int = 500
    code: str = "internal_error"

    def __init__(self, detail: str, *, field: str | None = None) -> None:
        """Store the human message and, optionally, the offending field name.

        :param detail: a human-readable description of what went wrong.
        :param field: the input field the error is attributed to, when one
            applies (e.g. a duplicate ``name``); ``None`` for whole-request
            errors.
        """
        super().__init__(detail)
        self.detail = detail
        self.field = field


class NotFoundError(AppError):
    """A referenced resource does not exist."""

    status = 404
    code = "not_found"


class ConflictError(AppError):
    """The write conflicts with existing state (e.g. a duplicate unique value)."""

    status = 409
    code = "conflict"


class ValidationError(AppError):
    """The request is well-formed but semantically invalid."""

    status = 422
    code = "unprocessable"


class PermissionError_(AppError):
    """The caller is authenticated but not permitted to perform the write.

    Named with a trailing underscore to avoid shadowing the builtin
    :class:`PermissionError`.
    """

    status = 403
    code = "forbidden"


class ServiceUnavailableError(AppError):
    """The write cannot proceed because a required component is unavailable."""

    status = 503
    code = "unavailable"

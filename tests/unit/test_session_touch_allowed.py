"""The passive-poll rule: which requests refresh the session idle clock (#70 F-S1).

:func:`session_touch_allowed` decides from two request headers that the
background pollers send (``poll.js``): ``X-TLM-Passive: 1`` marks the request as
a poll, and ``X-TLM-Idle`` carries the whole seconds since the page last saw
input. Only an unmarked request, a poll right after real input, or any poll
with ``session.keepalive_on_poll`` on, counts as activity.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from timelapse_manager.config.settings import SessionSettings
from timelapse_manager.security.authz import session_touch_allowed
from timelapse_manager.security.sessions import touch_interval

# idle 1800 s -> touch interval 60 s.
_SETTINGS = SessionSettings(idle_timeout_seconds=1800)
_INTERVAL = int(touch_interval(_SETTINGS).total_seconds())


def _request(headers: dict[str, str]) -> Request:
    raw = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
    ]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def test_interval_assumption() -> None:
    assert _INTERVAL == 60


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # Not a poll: always activity, whatever the idle header says.
        ({}, True),
        ({"X-TLM-Idle": "99999"}, True),
        ({"X-TLM-Passive": "0", "X-TLM-Idle": "99999"}, True),
        ({"X-TLM-Passive": "true", "X-TLM-Idle": "99999"}, True),
        ({"X-TLM-Passive": "", "X-TLM-Idle": "99999"}, True),
        # A poll with no usable idle value is "not recently active".
        ({"X-TLM-Passive": "1"}, False),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": ""}, False),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": "abc"}, False),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": "-5"}, False),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": "1.5"}, False),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": " 5"}, False),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": "1234567890"}, False),
        # Bounded ASCII digits only: no other Unicode digit forms.
        ({"X-TLM-Passive": "1", "X-TLM-Idle": "²"}, False),
        # A poll right after input counts; one interval or more does not.
        ({"X-TLM-Passive": "1", "X-TLM-Idle": "0"}, True),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": str(_INTERVAL - 1)}, True),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": str(_INTERVAL)}, False),
        ({"X-TLM-Passive": "1", "X-TLM-Idle": "3600"}, False),
    ],
)
def test_header_matrix(headers: dict[str, str], expected: bool) -> None:
    assert session_touch_allowed(_request(headers), _SETTINGS) is expected


@pytest.mark.parametrize("idle", [None, "abc", "0", "3600"])
def test_keepalive_on_poll_always_touches(idle: str | None) -> None:
    settings = SessionSettings(idle_timeout_seconds=1800, keepalive_on_poll=True)
    headers = {"X-TLM-Passive": "1"}
    if idle is not None:
        headers["X-TLM-Idle"] = idle
    assert session_touch_allowed(_request(headers), settings) is True


def test_interval_follows_a_short_idle_timeout() -> None:
    # idle 8 s -> interval 1 s: only a poll with idle 0 counts.
    settings = SessionSettings(idle_timeout_seconds=8)
    assert session_touch_allowed(
        _request({"X-TLM-Passive": "1", "X-TLM-Idle": "0"}), settings
    )
    assert not session_touch_allowed(
        _request({"X-TLM-Passive": "1", "X-TLM-Idle": "1"}), settings
    )


def test_keepalive_on_poll_defaults_off() -> None:
    assert SessionSettings().keepalive_on_poll is False

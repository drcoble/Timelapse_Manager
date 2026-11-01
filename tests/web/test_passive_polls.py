"""Background polls do not keep an idle session alive (#70 F-S1, WP-70-E1).

The pollers mark their requests ``X-TLM-Passive: 1`` with ``X-TLM-Idle`` (the
seconds since the page last saw input). Such a request is validated in full
but must not refresh ``last_active`` unless the user really was active just
now, or ``session.keepalive_on_poll`` is on.

There are two touch paths, and both must be gated:

* the auth dependency (``require_authenticated_session``) -- isolated by the
  JSON route ``/frames/since``, which renders no template;
* ``csrf_token_for``, run by ``base_context`` for every rendered page and
  partial -- covered by ``/alerts/summary``, ``/partials/projects`` and the
  full-page ``/renders`` poll, which pass through both.
"""

from __future__ import annotations

import datetime
from collections.abc import Generator
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_PEER, login, seed_admin
from timelapse_manager.app import create_app
from timelapse_manager.config.settings import SessionSettings, Settings
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.sessions import hash_token

_POLL_PATHS = [
    "/frames/since?after=0",
    "/alerts/summary",
    "/partials/projects",
    "/renders",
]
_PASSIVE = {"X-TLM-Passive": "1"}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _set_last_active(raw_token: str, value: datetime.datetime) -> None:
    with session_scope(get_context().session_factory) as db:
        row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
        row.last_active = value


def _last_active(raw_token: str) -> datetime.datetime | None:
    with session_scope(get_context().session_factory) as db:
        row = db.query(SessionRow).filter_by(token_hash=hash_token(raw_token)).one()
        return row.last_active


@pytest.fixture()
def signed_in(web_client: TestClient) -> tuple[TestClient, str]:
    """An admin session whose ``last_active`` is five minutes old."""
    seed_admin(web_client)
    token = login(web_client)
    return web_client, token


def _five_minutes_idle(token: str) -> datetime.datetime:
    stale = _utcnow() - datetime.timedelta(minutes=5)
    _set_last_active(token, stale)
    return stale


@pytest.mark.parametrize("path", _POLL_PATHS)
def test_passive_poll_does_not_touch(
    signed_in: tuple[TestClient, str], path: str
) -> None:
    client, token = signed_in
    stale = _five_minutes_idle(token)
    resp = client.get(path, headers=_PASSIVE)
    assert resp.status_code == 200
    assert _last_active(token) == stale


@pytest.mark.parametrize("path", _POLL_PATHS)
def test_passive_poll_right_after_input_touches(
    signed_in: tuple[TestClient, str], path: str
) -> None:
    client, token = signed_in
    stale = _five_minutes_idle(token)
    resp = client.get(path, headers={**_PASSIVE, "X-TLM-Idle": "5"})
    assert resp.status_code == 200
    touched = _last_active(token)
    assert touched is not None and touched > stale


@pytest.mark.parametrize("path", _POLL_PATHS)
def test_passive_poll_long_idle_does_not_touch(
    signed_in: tuple[TestClient, str], path: str
) -> None:
    client, token = signed_in
    stale = _five_minutes_idle(token)
    resp = client.get(path, headers={**_PASSIVE, "X-TLM-Idle": "300"})
    assert resp.status_code == 200
    assert _last_active(token) == stale


@pytest.mark.parametrize("path", _POLL_PATHS)
def test_unmarked_request_touches(signed_in: tuple[TestClient, str], path: str) -> None:
    client, token = signed_in
    stale = _five_minutes_idle(token)
    resp = client.get(path)
    assert resp.status_code == 200
    touched = _last_active(token)
    assert touched is not None and touched > stale


def test_idle_expired_passive_htmx_poll_redirects_to_login(
    signed_in: tuple[TestClient, str],
) -> None:
    client, token = signed_in
    stale = _utcnow() - datetime.timedelta(minutes=31)
    _set_last_active(token, stale)
    resp = client.get(
        "/alerts/summary",
        headers={
            **_PASSIVE,
            "X-TLM-Idle": "1860",
            "HX-Request": "true",
            "HX-Current-URL": "https://testserver/renders?page=2",
        },
    )
    assert resp.status_code == 204
    target = urlsplit(resp.headers["HX-Redirect"])
    assert target.path == "/login"
    assert parse_qs(target.query) == {"next": ["/renders?page=2"]}
    assert _last_active(token) == stale


def test_idle_expired_fetch_poll_gets_json_401(
    signed_in: tuple[TestClient, str],
) -> None:
    """The fetch pollers (poll.js) act on a plain 401, not a redirect."""
    client, token = signed_in
    _set_last_active(token, _utcnow() - datetime.timedelta(minutes=31))
    resp = client.get(
        "/frames/since?after=0",
        headers={**_PASSIVE, "X-Requested-With": "fetch", "Accept": "application/json"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")


@pytest.fixture()
def keepalive_client(
    web_settings: Settings, alembic_cfg: Config
) -> Generator[TestClient, None, None]:
    """A client on an app with ``session.keepalive_on_poll: true``."""
    settings = web_settings.model_copy(
        update={
            "session": web_settings.session.model_copy(
                update={"keepalive_on_poll": True}
            )
        }
    )
    assert isinstance(settings.session, SessionSettings)
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)
    alembic_command.upgrade(alembic_cfg, "head")
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver", client=LOOPBACK_PEER) as c:
        yield c


@pytest.mark.parametrize("path", _POLL_PATHS)
def test_keepalive_on_poll_touches(keepalive_client: TestClient, path: str) -> None:
    seed_admin(keepalive_client)
    token = login(keepalive_client)
    stale = _five_minutes_idle(token)
    resp = keepalive_client.get(path, headers={**_PASSIVE, "X-TLM-Idle": "300"})
    assert resp.status_code == 200
    touched = _last_active(token)
    assert touched is not None and touched > stale

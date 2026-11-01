"""The alerts panel is static; responses update it out of band (#70 F-R6).

The page header holds a static toggle (``#alerts-toggle``) and a static panel
(``#alerts-panel``, ``hidden`` until alerts.js opens it). ``/alerts/summary``
and both clear actions answer with out-of-band replacements for exactly
``#alerts-badge``, ``#alerts-count-label`` and ``#alerts-panel-body``, and never
with toggle markup, so no response can take the toggle's focus or close an
open panel.

Real keyboard, focus and touch behaviour is covered by
``tests/e2e/test_alerts_a11y.py``.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import log_event
from timelapse_manager.runtime import get_context

_OOB_IDS = ["alerts-badge", "alerts-count-label", "alerts-panel-body"]


class _TopLevel(HTMLParser):
    """Collect the attributes of each top-level element of a fragment."""

    _VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.top: list[dict[str, str]] = []
        self.all_ids: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        if "id" in attr:
            self.all_ids.append(attr["id"])
        if self._depth == 0:
            self.top.append({"tag": tag, **attr})
        if tag not in self._VOID:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._VOID:
            self._depth -= 1


def _parse(html: str) -> _TopLevel:
    parser = _TopLevel()
    parser.feed(html)
    return parser


def _seed_alert(message: str, level: str = "warning") -> int:
    with session_scope(get_context().session_factory) as db:
        log_event(db, scope="system", scope_id=None, level=level, message=message)
        db.flush()
        row = db.query(Event).filter(Event.message == message).one()
        return row.id


def _post(client: TestClient, path: str) -> str:
    resp = client.post(
        path,
        data={"csrf_token": csrf_of(client, "/")},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    return resp.text


def _assert_oob_only(html: str) -> None:
    parsed = _parse(html)
    assert [el.get("id") for el in parsed.top] == _OOB_IDS
    for el in parsed.top:
        assert el.get("hx-swap-oob") == "true", el
    assert "alerts-toggle" not in parsed.all_ids
    assert "alerts-panel" not in parsed.all_ids
    assert "alerts-area" not in parsed.all_ids
    assert "aria-expanded" not in html


class TestResponsesAreOutOfBand:
    def test_summary(self, admin_client: TestClient) -> None:
        _seed_alert("OOB-SUMMARY")
        resp = admin_client.get("/alerts/summary")
        assert resp.status_code == 200
        _assert_oob_only(resp.text)
        assert "OOB-SUMMARY" in resp.text

    def test_clear_one(self, operator_client: TestClient) -> None:
        cleared = _seed_alert("OOB-CLEAR-ME")
        _seed_alert("OOB-STAYS")
        html = _post(operator_client, f"/alerts/{cleared}/clear")
        _assert_oob_only(html)
        assert "OOB-CLEAR-ME" not in html
        assert "OOB-STAYS" in html

    def test_clear_all(self, operator_client: TestClient) -> None:
        _seed_alert("OOB-ALL-1")
        _seed_alert("OOB-ALL-2", level="error")
        html = _post(operator_client, "/alerts/clear-all")
        _assert_oob_only(html)
        assert "No active alerts." in html

    @pytest.mark.parametrize("count", [0, 2])
    def test_badge_target_is_always_present(
        self, admin_client: TestClient, count: int
    ) -> None:
        """At zero the badge is an empty, unstyled span, so the next count lands."""
        for n in range(count):
            _seed_alert(f"OOB-BADGE-{n}")
        badge = _parse(admin_client.get("/alerts/summary").text).top[0]
        assert badge["id"] == "alerts-badge"
        assert badge["aria-hidden"] == "true"
        if count:
            assert badge.get("class") == "alerts-badge"
        else:
            assert "class" not in badge

    def test_count_label_names_the_count(self, admin_client: TestClient) -> None:
        _seed_alert("OOB-LABEL")
        assert (
            '<span id="alerts-count-label" class="visually-hidden" '
            'hx-swap-oob="true">Active alerts: 1</span>'
        ) in admin_client.get("/alerts/summary").text

    def test_clear_controls_swap_nothing_themselves(
        self, operator_client: TestClient
    ) -> None:
        _seed_alert("OOB-CONTROLS")
        html = operator_client.get("/alerts/summary").text
        assert 'hx-post="/alerts/clear-all"' in html
        assert "hx-target" not in html
        assert html.count('hx-swap="none"') == 2  # Clear all + one Dismiss


class TestHeaderMarkup:
    def _header(self, client: TestClient) -> str:
        page = client.get("/").text
        start = page.index('<div class="alerts-menu">')
        return page[start : page.index("</header>", start)]

    def test_static_disclosure(self, admin_client: TestClient) -> None:
        header = self._header(admin_client)
        toggle = header[
            header.index("<button") : header.index(">", header.index("<button"))
        ]
        assert 'id="alerts-toggle"' in toggle
        assert 'aria-expanded="false"' in toggle
        assert 'aria-controls="alerts-panel"' in toggle
        # The name comes from the OOB-updated label, not a static aria-label,
        # and it is a disclosure, not a menu.
        assert "aria-label" not in toggle
        assert "aria-haspopup" not in toggle
        assert 'id="alerts-count-label"' in header
        assert 'id="alerts-badge"' in header
        panel = header[header.index('<div id="alerts-panel"') :]
        panel = panel[: panel.index(">")]
        assert " hidden" in panel
        assert 'role="region"' in panel
        assert 'id="alerts-panel-body"' in header

    def test_poller_is_separate_and_swaps_nothing(
        self, admin_client: TestClient
    ) -> None:
        header = self._header(admin_client)
        start = header.index('<div id="alerts-area"')
        poller = header[start : header.index(">", start)]
        assert "data-poll" in poller
        assert 'hx-get="/alerts/summary"' in poller
        assert 'hx-swap="none"' in poller
        assert (
            'hx-trigger="load, every 30s, tlm:visible from:document, tlm:refresh"'
            in poller
        )
        assert header[start:].index("</div>") == len(poller) + 1  # no children

    def test_alerts_js_loads_after_poll_js(self, admin_client: TestClient) -> None:
        page = admin_client.get("/").text
        assert page.index("/static/js/poll.js") < page.index("/static/js/alerts.js")

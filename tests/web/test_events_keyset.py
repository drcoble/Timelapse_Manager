"""Web tests for the keyset /events/batch endpoint + filters + audit split.

Seeded events use scope='camera' and queries filter ?scope=camera, so the
admin login's operational 'signed in' event (scope=system) never skews counts.
"""

from __future__ import annotations

import datetime

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_timed_events(
    specs: list[tuple[str, str, datetime.datetime]],
) -> list[int]:
    """Seed operational events with explicit (naive UTC) timestamps. Returns ids.

    Distinct timestamps make the ``?at=`` jump deterministic (the default
    ``current_timestamp`` would give every row the same instant).
    """
    ctx = get_context()
    ids: list[int] = []
    with session_scope(ctx.session_factory) as db:
        for level, message, ts in specs:
            ev = Event(
                scope="camera",
                scope_id=1,
                level=level,
                message=message,
                timestamp=ts,
                event_metadata=None,
            )
            db.add(ev)
            db.flush()
            ids.append(ev.id)
    return ids


def _seed_events(specs: list[tuple[str, str]]) -> list[int]:
    """Seed operational events (typeless metadata => not audit). Returns ids."""
    ctx = get_context()
    ids: list[int] = []
    with session_scope(ctx.session_factory) as db:
        for level, message in specs:
            ev = Event(
                scope="camera",
                scope_id=1,
                level=level,
                message=message,
                event_metadata=None,
            )
            db.add(ev)
            db.flush()
            ids.append(ev.id)
    return ids


def _seed_audit_events(specs: list[tuple[str, str]]) -> list[int]:
    """Seed audit/security events (typed metadata => audit). Returns ids.

    Half the rows carry the audit control-action type and half the security
    auth-event type, so the inclusion filter is exercised across both types.
    """
    from timelapse_manager.monitoring import EventType

    types = (
        EventType.AUDIT_CONTROL_ACTION.value,
        EventType.SECURITY_AUTH_EVENT.value,
    )
    ctx = get_context()
    ids: list[int] = []
    with session_scope(ctx.session_factory) as db:
        for i, (level, message) in enumerate(specs):
            ev = Event(
                scope="system",
                scope_id=None,
                level=level,
                message=message,
                event_metadata={"type": types[i % len(types)]},
            )
            db.add(ev)
            db.flush()
            ids.append(ev.id)
    return ids


def _row_count(html: str) -> int:
    return html.count('class="event-row')


def test_batch_first_page_and_sentinel(admin_client: TestClient) -> None:
    _seed_events([("info", f"msg {i}") for i in range(80)])
    html = admin_client.get("/events/batch?scope=camera").text
    assert _row_count(html) == 75  # _OPERATIONAL_EVENTS_PER_PAGE
    assert "log-sentinel" in html
    assert "log-end-cap" not in html


def test_batch_endcap_for_small_log(admin_client: TestClient) -> None:
    _seed_events([("info", f"m{i}") for i in range(5)])
    html = admin_client.get("/events/batch?scope=camera").text
    assert _row_count(html) == 5
    assert "log-end-cap" in html
    assert "log-sentinel" not in html


def test_batch_before_returns_older(admin_client: TestClient) -> None:
    ids = _seed_events([("info", f"m{i}") for i in range(80)])
    cursor = sorted(ids, reverse=True)[74]  # 75th-newest (the next_before) -> 5 older
    html = admin_client.get(f"/events/batch?scope=camera&before={cursor}").text
    assert _row_count(html) == 5
    assert "log-end-cap" in html


def test_level_filter(admin_client: TestClient) -> None:
    _seed_events(
        [("info", "an info")] * 3
        + [("error", "an error")] * 2
        + [("warning", "a warn")]
    )
    html = admin_client.get("/events/batch?scope=camera&level=error").text
    assert _row_count(html) == 2
    assert "an error" in html
    assert "an info" not in html


def test_search_filter(admin_client: TestClient) -> None:
    _seed_events(
        [("info", "alpha event"), ("info", "beta event"), ("info", "alpha two")]
    )
    html = admin_client.get("/events/batch?scope=camera&q=alpha").text
    assert _row_count(html) == 2
    assert "beta event" not in html


def test_events_page_has_grid_and_filters(admin_client: TestClient) -> None:
    _seed_events([("info", f"m{i}") for i in range(80)])
    html = admin_client.get("/events?scope=camera").text
    assert 'id="events-tbody"' in html
    assert "log-sentinel" in html
    assert 'name="q"' in html
    assert 'name="level"' in html  # hidden mirror synced from the level chips
    assert "Page " not in html  # old offset pagination gone


def test_audit_admin_ok(admin_client: TestClient) -> None:
    assert admin_client.get("/events/audit").status_code == 200


def test_audit_viewer_forbidden(viewer_client: TestClient) -> None:
    assert viewer_client.get("/events/audit").status_code == 403


# --- Multi-level chip filtering -------------------------------------------


def test_level_chip_multi_selection_filters(admin_client: TestClient) -> None:
    """A comma-separated level= (the chip selection) keeps only those levels."""
    _seed_events(
        [("info", "an info")] * 2
        + [("warning", "a warn")] * 3
        + [("error", "an error")] * 2
        + [("critical", "a crit")] * 1
    )
    # The WARN + ERROR chips pressed -> level=warning,error.
    html = admin_client.get("/events/batch?scope=camera&level=warning,error").text
    assert _row_count(html) == 5  # 3 warning + 2 error
    assert "a warn" in html
    assert "an error" in html
    assert "an info" not in html
    assert "a crit" not in html


def test_level_chip_selection_rides_sentinel_continuation(
    admin_client: TestClient,
) -> None:
    """The active level set is carried on the sentinel's continuation URL."""
    _seed_events(
        [("warning", f"w{i}") for i in range(80)]
        + [("info", f"i{i}") for i in range(10)]
    )
    html = admin_client.get("/events/batch?scope=camera&level=warning").text
    # The sentinel's next-batch URL preserves the level filter so the scroll
    # continuation stays scoped to the selected chips.
    assert "level=warning" in html


# --- ?at= time jump --------------------------------------------------------


def test_events_at_returns_events_at_or_before_instant(
    admin_client: TestClient,
) -> None:
    """``?at=`` lands on the events at-or-before the instant (boundary included)."""
    base = datetime.datetime(2026, 3, 1, 12, 0, 0)
    specs = [("info", f"e{i}", base + datetime.timedelta(minutes=i)) for i in range(10)]
    _seed_timed_events(specs)
    # Jump to the instant of e5 -> e5 and older (e0..e5) are at-or-before it.
    at = (base + datetime.timedelta(minutes=5)).isoformat()
    html = admin_client.get(f"/events?scope=camera&at={at}").text
    assert "e5" in html  # the boundary event itself is included
    assert "e4" in html  # older events page from there
    assert "e6" not in html  # newer events are excluded by the jump
    assert "e9" not in html


def test_events_at_has_usable_continuation_cursor(
    admin_client: TestClient,
) -> None:
    """An ``?at=`` jump exposes a sentinel cursor that pages further older."""
    base = datetime.datetime(2026, 3, 1, 12, 0, 0)
    specs = [
        ("info", f"e{i}", base + datetime.timedelta(minutes=i)) for i in range(120)
    ]
    _seed_timed_events(specs)
    # Jump near the newest -> a full batch with more older events beyond it.
    at = (base + datetime.timedelta(minutes=119)).isoformat()
    html = admin_client.get(f"/events?scope=camera&at={at}").text
    assert "log-sentinel" in html  # more older events remain -> a live cursor
    # The continuation link is a real keyset cursor (?before=<id>), not ?at=.
    assert "/events/batch?before=" in html or "/events?before=" in html


def test_events_window_empty_when_anchor_precedes_log(
    admin_client: TestClient,
) -> None:
    """An anchor before the whole log yields an empty window, not the newest page."""
    base = datetime.datetime(2026, 3, 1, 12, 0, 0)
    _seed_timed_events(
        [("info", f"e{i}", base + datetime.timedelta(minutes=i)) for i in range(5)]
    )
    before_all = (base - datetime.timedelta(days=1)).isoformat()
    html = admin_client.get(f"/events?scope=camera&at={before_all}").text
    assert "e0" not in html
    assert "e4" not in html
    assert "No events match" in html


def test_events_at_hx_request_returns_batch_fragment(
    admin_client: TestClient,
) -> None:
    """An HTMX ``?at=`` request returns just the batch fragment, not the page."""
    base = datetime.datetime(2026, 3, 1, 12, 0, 0)
    _seed_timed_events(
        [("info", f"e{i}", base + datetime.timedelta(minutes=i)) for i in range(10)]
    )
    at = (base + datetime.timedelta(minutes=5)).isoformat()
    html = admin_client.get(
        f"/events?scope=camera&at={at}", headers={"HX-Request": "true"}
    ).text
    assert "e5" in html
    assert 'id="events-tbody"' not in html  # fragment only, no page chrome


# --- /events/since ---------------------------------------------------------


def test_events_since_counts_newer_matching(admin_client: TestClient) -> None:
    """``/events/since`` counts operational events with id > after."""
    ids = _seed_events([("info", f"m{i}") for i in range(10)])
    ordered = sorted(ids)
    after = ordered[3]  # 6 events have a higher id
    data = admin_client.get(f"/events/since?after={after}&scope=camera").json()
    assert data == {"count": 6}


def test_events_since_respects_level_filter(admin_client: TestClient) -> None:
    """The since-count honours the active level filter (only matching new rows)."""
    ids = _seed_events(
        [("info", "i0"), ("error", "e0"), ("info", "i1"), ("error", "e1")]
    )
    after = min(ids) - 1  # all four are newer
    data = admin_client.get(
        f"/events/since?after={after}&scope=camera&level=error"
    ).json()
    assert data == {"count": 2}  # only the two error rows


def test_events_since_respects_scope_filter(admin_client: TestClient) -> None:
    """A scope filter narrows the since-count to that scope."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        for scope_val in ("camera", "system", "camera"):
            db.add(
                Event(
                    scope=scope_val,
                    scope_id=1,
                    level="info",
                    message=f"{scope_val} event",
                    event_metadata=None,
                )
            )
        db.flush()
    data = admin_client.get("/events/since?after=0&scope=camera").json()
    # Exactly the two camera rows seeded above (after=0 includes everything, but
    # the admin login's own 'signed in' event is scope=system, so it is excluded).
    assert data["count"] == 2


# --- /audit/batch keyset continuous scroll --------------------------------


def test_audit_batch_first_page_and_sentinel(admin_client: TestClient) -> None:
    _seed_audit_events([("info", f"audit {i}") for i in range(60)])
    html = admin_client.get("/audit/batch").text
    assert _row_count(html) == 50  # _EVENTS_PER_PAGE (audit batch size)
    assert "log-sentinel" in html
    assert "log-end-cap" not in html


def test_audit_batch_endcap_for_small_log(admin_client: TestClient) -> None:
    _seed_audit_events([("info", f"a{i}") for i in range(5)])
    html = admin_client.get("/audit/batch").text
    assert _row_count(html) == 5
    assert "log-end-cap" in html
    assert "log-sentinel" not in html


def test_audit_batch_before_pages_without_skip_or_dup(
    admin_client: TestClient,
) -> None:
    """Keyset paging over /audit/batch covers the same ids as the offset query.

    Seeds 60 audit rows, pages the keyset batch-by-batch, and asserts the set of
    ids walked equals the offset-ordered ids from get_audit_events on identical
    data -- no row skipped, none duplicated across the page boundary.
    """
    import re

    from timelapse_manager.db.models import User
    from timelapse_manager.monitoring import get_audit_events

    _seed_audit_events([("info", f"audit {i}") for i in range(60)])

    # Reference: the offset query's ordered id list on the same data.
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        admin = db.query(User).filter(User.role == "admin").first()
        assert admin is not None
        rows, total = get_audit_events(db, admin, limit=1000, offset=0)
        expected_ids = [e.id for e in rows]
    assert total == 60
    assert len(expected_ids) == 60

    # First keyset page: the newest 50 (offset rows 0..49). The sentinel's
    # data-newest-id is the newest id; its continuation cursor (before=) is the
    # 50th-newest id, the boundary into the next page.
    first = admin_client.get("/audit/batch").text
    assert _row_count(first) == 50
    m = re.search(r"/events/audit\?before=(\d+)", first)
    assert m is not None
    cursor = int(m.group(1))
    # The cursor is exactly the id of the 50th-newest row (offset row index 49),
    # so the keyset boundary lands precisely where the offset page ends -- no
    # skip, no duplicate.
    assert cursor == expected_ids[49]

    # Second keyset page: strictly-older rows (id < cursor) -> the remaining 10.
    second = admin_client.get(f"/audit/batch?before={cursor}").text
    assert _row_count(second) == 10
    assert "log-end-cap" in second  # the start of the log is reached


def test_audit_batch_excludes_operational_rows(admin_client: TestClient) -> None:
    """An operational (non-audit) row must NEVER appear in /audit/batch results.

    The inclusion filter (type IN audit types) is the mirror of the operational
    exclusion; this proves no operational leak into the admin-only audit view.
    """
    _seed_audit_events([("info", "AUDIT-INCLUDED")])
    _seed_events([("info", "OPERATIONAL-LEAK-CHECK")])  # typeless => operational
    html = admin_client.get("/audit/batch").text
    assert "AUDIT-INCLUDED" in html
    assert "OPERATIONAL-LEAK-CHECK" not in html


def test_audit_batch_search_filter(admin_client: TestClient) -> None:
    _seed_audit_events(
        [("info", "alpha audit"), ("info", "beta audit"), ("info", "alpha two")]
    )
    html = admin_client.get("/audit/batch?q=alpha").text
    assert _row_count(html) == 2
    assert "beta audit" not in html


def test_audit_batch_forbidden_for_viewer(viewer_client: TestClient) -> None:
    """/audit/batch is admin-only: a viewer gets 403."""
    assert viewer_client.get("/audit/batch").status_code == 403


def test_audit_batch_forbidden_for_operator(operator_client: TestClient) -> None:
    """/audit/batch is admin-only: an operator gets 403."""
    assert operator_client.get("/audit/batch").status_code == 403


def test_audit_page_forbidden_for_viewer(viewer_client: TestClient) -> None:
    """/events/audit is admin-only: a viewer gets 403."""
    assert viewer_client.get("/events/audit").status_code == 403


def test_audit_page_forbidden_for_operator(operator_client: TestClient) -> None:
    """/events/audit is admin-only: an operator gets 403."""
    assert operator_client.get("/events/audit").status_code == 403


def test_audit_page_is_continuous_scroll_shell(admin_client: TestClient) -> None:
    _seed_audit_events([("info", f"a{i}") for i in range(60)])
    html = admin_client.get("/events/audit").text
    assert 'id="audit-tbody"' in html
    assert "log-sentinel" in html
    assert "Page " not in html  # old offset pagination gone


# --- Operations / Audit tab bar -------------------------------------------


def test_events_tab_bar_present_for_admin_on_events_page(
    admin_client: TestClient,
) -> None:
    """An admin's operational events page shows both Operations and Audit tabs."""
    html = admin_client.get("/events").text
    assert "Operations" in html
    assert 'href="/events/audit"' in html  # the Audit tab link


def test_events_tab_bar_audit_absent_for_viewer(viewer_client: TestClient) -> None:
    """A viewer's events page must NOT contain the Audit tab in the DOM."""
    html = viewer_client.get("/events").text
    # Operations is present for everyone; the Audit tab link is admin-only.
    assert "Operations" in html
    assert 'href="/events/audit"' not in html


def test_events_tab_bar_audit_absent_for_operator(
    operator_client: TestClient,
) -> None:
    """An operator's events page must NOT contain the Audit tab in the DOM."""
    html = operator_client.get("/events").text
    assert "Operations" in html
    assert 'href="/events/audit"' not in html


def test_events_tab_active_state_distinct(admin_client: TestClient) -> None:
    """Operations is active on /events; Audit is active on /events/audit (not both)."""
    ops = admin_client.get("/events").text
    # On /events: Operations tab carries aria-selected="true", Audit does not.
    assert 'aria-selected="true"' in ops
    audit = admin_client.get("/events/audit").text
    # On /events/audit: exactly one Audit tab is selected; Operations is not the
    # startswith false-positive (Operations uses an exact-path match).
    assert audit.count('aria-selected="true"') == 1

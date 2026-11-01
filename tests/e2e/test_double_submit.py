"""Mutation safety on the frames page (#70 F-R2, WP-70-C).

Drives the real page in Chromium against a live server:

* a double click on **Apply offset** sends one POST and shifts the timestamps
  once (checked in the database, not just by counting requests);
* a double click on **Undo** sends one POST and restores the original time;
* a double click on **Export** sends one POST and enqueues one job;
* the export poll stops after three consecutive failures, at once on a 401,
  and resumes after a page reload;
* a bulk action that gets a 500 announces that nothing changed;
* the Idempotency-Key fallback works where ``crypto.randomUUID`` is missing
  (plain-HTTP access to the VM is not a secure context).

Requests are held with ``page.route`` (the handler keeps the ``Route`` and
returns), never with a sleep inside the handler: a sleeping handler blocks
Playwright's dispatch, so the second click could only arrive after the first
request had finished, and the test would prove nothing.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import _boot_app, login, make_frame_seeder, stop_app

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_HOLD_MS = 400
_FRAMES_URL = "/frames?project_id=1"
_FAKE_JOB = 4242
_RESULT_BAR = "#frames-action-bar.frames-action-bar--result"
_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@pytest.fixture()
def frames_env(
    tmp_path: Path, _chromium_page: object
) -> Generator[tuple[Any, str, Path], None, None]:
    """A signed-in page on a server seeded with project 1 and three frames.

    Yields ``(page, base_url, db_path)``; the database path lets a test check
    what actually changed, not just what the browser sent.
    """
    proc, base_url = _boot_app(
        tmp_path, extra_seed=make_frame_seeder(3, project_name="Double Submit")
    )
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url, tmp_path / "e2e_test.db"
    finally:
        stop_app(proc)


def _query(db_path: Path, sql: str, *args: object) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _capture_ts(db_path: Path, frame_id: int) -> datetime:
    rows = _query(db_path, "SELECT capture_timestamp FROM frame WHERE id = ?", frame_id)
    return datetime.fromisoformat(str(rows[0][0]))


def _export_job_count(db_path: Path) -> int:
    rows = _query(db_path, "SELECT COUNT(*) FROM render_job WHERE kind = 'export'")
    return int(rows[0][0])


def _open_frames_and_select_first(page: Any, base: str) -> int:
    page.goto(f"{base}{_FRAMES_URL}", wait_until="domcontentloaded")
    label = page.locator(".frame-tile-select").first
    label.wait_for(timeout=_TIMEOUT)
    # The checkbox carries no id (#198); the tile's id is "frame-tile-<id>".
    tile_id = page.locator(".frame-tile").first.get_attribute("id")
    frame_id = int(tile_id.removeprefix("frame-tile-"))
    label.click()
    page.locator("#frames-action-bar").wait_for(state="visible", timeout=_TIMEOUT)
    return frame_id


def _record_posts(page: Any, path: str) -> list[Any]:
    posts: list[Any] = []

    def on_request(request: Any) -> None:
        if request.method == "POST" and request.url.split("?")[0].endswith(path):
            posts.append(request)

    page.on("request", on_request)
    return posts


def _hold(page: Any, pattern: str) -> list[Any]:
    """Hold every matching request until the test continues it."""
    held: list[Any] = []
    page.route(pattern, lambda route: held.append(route))
    return held


def _release_all(page: Any, pattern: str, held: list[Any]) -> None:
    # Continue first: unroute() itself settles still-pending routes.
    for route in held:
        route.continue_()
    page.unroute(pattern)


def _apply_one_hour_offset(page: Any) -> None:
    page.locator("[data-offset-toggle]").click()
    page.locator("#frames-offset-panel").wait_for(state="visible", timeout=_TIMEOUT)
    page.locator("#frames-offset-panel [data-offset-h]").fill("1")


class TestOffsetDoubleSubmit:
    def test_double_click_apply_shifts_once(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, db_path = frames_env
        frame_id = _open_frames_and_select_first(page, base)
        before = _capture_ts(db_path, frame_id)
        _apply_one_hour_offset(page)

        posts = _record_posts(page, "/frames/offset")
        held = _hold(page, "**/frames/offset")
        page.locator("[data-offset-apply]").dblclick()
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1, "the second click of the double click sent a request"
        assert page.locator("[data-offset-apply]").is_disabled()

        _release_all(page, "**/frames/offset", held)
        page.locator(f"{_RESULT_BAR} [data-offset-undo]").wait_for(timeout=_TIMEOUT)
        page.wait_for_timeout(_HOLD_MS)

        assert len(posts) == 1
        assert posts[0].headers.get("idempotency-key")
        assert _capture_ts(db_path, frame_id) == before + timedelta(hours=1)
        assert page.locator("[data-offset-apply]").is_enabled()

    def test_double_click_undo_restores_once(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, db_path = frames_env
        frame_id = _open_frames_and_select_first(page, base)
        before = _capture_ts(db_path, frame_id)
        _apply_one_hour_offset(page)
        page.locator("[data-offset-apply]").click()
        undo = page.locator(f"{_RESULT_BAR} [data-offset-undo]")
        undo.wait_for(timeout=_TIMEOUT)
        assert _capture_ts(db_path, frame_id) == before + timedelta(hours=1)

        posts = _record_posts(page, "/frames/offset")
        held = _hold(page, "**/frames/offset")
        undo.dblclick()
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1

        _release_all(page, "**/frames/offset", held)
        page.wait_for_function(
            "() => /Offset undone/.test("
            "document.getElementById('frame-action-status').textContent)",
            timeout=_TIMEOUT,
        )
        page.wait_for_timeout(_HOLD_MS)
        assert len(posts) == 1
        assert _capture_ts(db_path, frame_id) == before

    def test_failed_undo_keeps_the_undo_button(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, db_path = frames_env
        frame_id = _open_frames_and_select_first(page, base)
        before = _capture_ts(db_path, frame_id)
        _apply_one_hour_offset(page)
        page.locator("[data-offset-apply]").click()
        undo = page.locator(f"{_RESULT_BAR} [data-offset-undo]")
        undo.wait_for(timeout=_TIMEOUT)

        page.route(
            "**/frames/offset",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"detail": "Internal Server Error"}),
            ),
        )
        undo.click()
        page.locator("#toast-region .toast[role=alert]").first.wait_for(
            timeout=_TIMEOUT
        )
        assert undo.is_visible()
        assert undo.is_enabled()
        assert _capture_ts(db_path, frame_id) == before + timedelta(hours=1)


class TestOffsetCsrfFailure:
    def test_csrf_403_offers_reload_and_frees_apply(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        """A fetch never runs HX-Trigger, so net.js raises the same event."""
        page, base, db_path = frames_env
        frame_id = _open_frames_and_select_first(page, base)
        before = _capture_ts(db_path, frame_id)
        _apply_one_hour_offset(page)
        page.route(
            "**/frames/offset",
            lambda route: route.fulfill(
                status=403, content_type="text/plain", body="CSRF validation failed."
            ),
        )
        page.locator("[data-offset-apply]").click()

        toast = page.locator(
            "#toast-region .toast[role=alert]", has_text="session changed"
        ).first
        toast.wait_for(timeout=_TIMEOUT)
        assert toast.locator(".toast-action", has_text="Reload").count() == 1
        page.wait_for_function(
            "() => !document.querySelector('[data-offset-apply]').disabled",
            timeout=_TIMEOUT,
        )
        assert _capture_ts(db_path, frame_id) == before


class TestExportDoubleSubmit:
    def test_double_click_export_enqueues_once(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, db_path = frames_env
        _open_frames_and_select_first(page, base)

        posts = _record_posts(page, "/frames/export")
        held = _hold(page, "**/frames/export")
        page.locator("#frames-action-bar [data-export-action]").dblclick()
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1

        _release_all(page, "**/frames/export", held)
        page.locator(f"{_RESULT_BAR}[data-export-job]").wait_for(timeout=_TIMEOUT)
        page.wait_for_timeout(_HOLD_MS)
        assert len(posts) == 1
        assert _export_job_count(db_path) == 1


def _fake_export_start(page: Any) -> None:
    """Answer the export POST with a fixed job handle (no real job)."""
    page.route(
        "**/frames/export",
        lambda route: route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps({"job_id": _FAKE_JOB, "status": "pending"}),
        ),
    )


def _serve_export_status(page: Any, polls: list[int], status: int, body: dict) -> None:
    def handler(route: Any) -> None:
        polls.append(status)
        route.fulfill(
            status=status, content_type="application/json", body=json.dumps(body)
        )

    page.route(f"**/frames/export/{_FAKE_JOB}", handler)


def _start_fake_export(page: Any, base: str) -> None:
    _open_frames_and_select_first(page, base)
    _fake_export_start(page)
    page.locator("#frames-action-bar [data-export-action]").click()
    page.locator(f"{_RESULT_BAR}[data-export-job='{_FAKE_JOB}']").wait_for(
        timeout=_TIMEOUT
    )


def _stored_export(page: Any) -> object:
    return page.evaluate("() => sessionStorage.getItem('tlm.export.1')")


class TestExportPoll:
    def test_stops_after_three_failures(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, _ = frames_env
        polls: list[int] = []
        _serve_export_status(page, polls, 500, {"detail": "Internal Server Error"})
        _start_fake_export(page, base)

        page.locator(_RESULT_BAR, has_text="Export status unavailable").wait_for(
            timeout=_TIMEOUT
        )
        page.wait_for_timeout(5_000)  # more than two poll intervals
        assert len(polls) == 3
        assert _stored_export(page) is None
        page.locator("#toast-region .toast[role=alert]").first.wait_for(
            timeout=_TIMEOUT
        )

    def test_stops_at_once_on_401(self, frames_env: tuple[Any, str, Path]) -> None:
        page, base, _ = frames_env
        polls: list[int] = []
        _serve_export_status(page, polls, 401, {"detail": "Not authenticated"})
        _start_fake_export(page, base)

        page.locator(_RESULT_BAR, has_text="Export status unavailable").wait_for(
            timeout=_TIMEOUT
        )
        page.wait_for_timeout(5_000)
        assert len(polls) == 1
        assert _stored_export(page) is None

    def test_resumes_after_reload_then_finishes(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, _ = frames_env
        polls: list[int] = []
        pending = {"job_id": _FAKE_JOB, "status": "pending", "progress": 0}
        _serve_export_status(page, polls, 200, pending)
        _start_fake_export(page, base)
        page.wait_for_function(
            "() => sessionStorage.getItem('tlm.export.1') !== null", timeout=_TIMEOUT
        )

        page.reload(wait_until="domcontentloaded")
        page.locator(
            f"{_RESULT_BAR}[data-export-job='{_FAKE_JOB}']", has_text="Preparing export"
        ).wait_for(timeout=_TIMEOUT)
        polled_after_reload = len(polls)
        deadline = time.monotonic() + 10.0
        while len(polls) <= polled_after_reload and time.monotonic() < deadline:
            page.wait_for_timeout(200)  # lets Playwright dispatch the route handler
        assert len(polls) > polled_after_reload, "the poll did not resume after reload"

        page.unroute(f"**/frames/export/{_FAKE_JOB}")
        done = {"job_id": _FAKE_JOB, "status": "done", "progress": 100}
        _serve_export_status(page, polls, 200, done)
        link = page.locator(f"{_RESULT_BAR} a[download]")
        link.wait_for(timeout=_TIMEOUT)
        assert link.get_attribute("href") == f"/frames/export/{_FAKE_JOB}/download"
        assert _stored_export(page) is None

    def test_dismiss_stops_and_forgets(self, frames_env: tuple[Any, str, Path]) -> None:
        page, base, _ = frames_env
        polls: list[int] = []
        pending = {"job_id": _FAKE_JOB, "status": "pending", "progress": 0}
        _serve_export_status(page, polls, 200, pending)
        _start_fake_export(page, base)

        page.locator(f"{_RESULT_BAR} [data-export-dismiss]").click()
        assert _stored_export(page) is None
        count = len(polls)
        page.wait_for_timeout(4_500)
        assert len(polls) == count
        # The next selection shows the selection bar's buttons again, enabled.
        page.locator(".frame-tile-select").first.click()
        export_btn = page.locator("#frames-action-bar [data-export-action]")
        export_btn.wait_for(state="visible", timeout=_TIMEOUT)
        assert export_btn.is_enabled()


class TestBulkFailure:
    def test_bulk_500_announces_nothing_changed(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, _ = frames_env
        _open_frames_and_select_first(page, base)
        page.route(
            "**/frames/bulk",
            lambda route: route.fulfill(
                status=500, content_type="text/plain", body="Internal Server Error"
            ),
        )
        page.locator('#frames-action-bar [data-bulk-action="exclude"]').click()
        page.wait_for_function(
            "() => /nothing was changed/.test("
            "document.getElementById('frame-action-status').textContent)",
            timeout=_TIMEOUT,
        )


class TestIdempotencyKeyFallback:
    def test_key_without_random_uuid(self, frames_env: tuple[Any, str, Path]) -> None:
        page, base, _ = frames_env
        page.add_init_script("delete Crypto.prototype.randomUUID;")
        page.goto(f"{base}{_FRAMES_URL}", wait_until="domcontentloaded")
        keys = page.evaluate(
            "() => [typeof crypto.randomUUID, tlmIdempotencyKey(), tlmIdempotencyKey()]"
        )
        assert keys[0] == "undefined"
        assert _UUID_V4.match(keys[1])
        assert keys[1] != keys[2]


# ---------------------------------------------------------------------------
# #70 WP-70-D: plain-form submit guard (forms.js) and the bulk Delete confirm.
# ---------------------------------------------------------------------------


def _hold_posts(page: Any, pattern: str) -> list[Any]:
    """Hold matching POSTs (a plain form's navigation included); pass the rest."""
    held: list[Any] = []

    def handler(route: Any) -> None:
        if route.request.method == "POST":
            held.append(route)
        else:
            route.continue_()

    page.route(pattern, handler)
    return held


# Chromium itself drops the second submit of a fast double click on a plain
# form, so ``dblclick`` passes with or without the guard. A second click 150 ms
# later (a hesitant double click, or a click on a slow page) does start a second
# POST without forms.js (measured: 2 POSTs for gaps of 50-300 ms), so the tests
# click twice with raw mouse events at that gap. Raw events also keep working
# while the first navigation is pending, when locator actions block.
_SECOND_CLICK_GAP_MS = 150


def _click_twice(page: Any, locator: Any) -> None:
    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.click(x, y)
    page.wait_for_timeout(_SECOND_CLICK_GAP_MS)
    page.mouse.click(x, y)


def _project_count(db_path: Path) -> int:
    return int(_query(db_path, "SELECT COUNT(*) FROM project")[0][0])


def _manual_render_count(db_path: Path) -> int:
    rows = _query(db_path, "SELECT COUNT(*) FROM render_job WHERE kind = 'manual'")
    return int(rows[0][0])


class TestPlainFormDoubleSubmit:
    def test_second_click_on_create_project_posts_once(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, db_path = frames_env
        before = _project_count(db_path)
        page.goto(f"{base}/projects/new", wait_until="domcontentloaded")
        page.fill("#name", "Double Click Project")
        page.fill("#capture_interval_value", "5")

        posts = _record_posts(page, "/projects")
        held = _hold_posts(page, "**/projects")
        submit = page.locator("button[type=submit]", has_text="Create Project")
        _click_twice(page, submit)
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1, "the second click sent a second POST"
        # (A pending navigation blocks DOM queries, so the button's disabled
        # state is not read here; the single held request is the proof.)

        with page.expect_navigation(wait_until="domcontentloaded"):
            _release_all(page, "**/projects", held)
        page.wait_for_timeout(_HOLD_MS)
        assert len(posts) == 1
        assert _project_count(db_path) == before + 1

    def test_second_click_on_render_now_posts_once(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, db_path = frames_env
        page.goto(f"{base}/projects/1", wait_until="domcontentloaded")
        page.click('.tab-item[data-tab-target="#tab-renders"]')
        page.click(".render-trigger-details > summary")
        submit = page.locator("form.render-trigger-panel button[type=submit]")
        submit.wait_for(timeout=_TIMEOUT)

        posts = _record_posts(page, "/projects/1/renders")
        held = _hold_posts(page, "**/projects/1/renders")
        _click_twice(page, submit)
        page.wait_for_timeout(_HOLD_MS)
        assert len(held) == 1, "the second click sent a second POST"
        # (A pending navigation blocks DOM queries, so the button's disabled
        # state is not read here; the single held request is the proof.)

        with page.expect_navigation(wait_until="domcontentloaded"):
            _release_all(page, "**/projects/1/renders", held)
        page.wait_for_timeout(_HOLD_MS)
        assert len(posts) == 1
        assert _manual_render_count(db_path) == 1

    def test_back_forward_restore_unlocks_the_form(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        page, base, _ = frames_env
        page.goto(f"{base}/projects/1", wait_until="domcontentloaded")
        page.click('.tab-item[data-tab-target="#tab-renders"]')
        page.click(".render-trigger-details > summary")
        submit = page.locator("form.render-trigger-panel button[type=submit]")
        submit.wait_for(timeout=_TIMEOUT)
        page.evaluate(
            """() => {
                const form = document.querySelector("form.render-trigger-panel");
                form.setAttribute("data-tlm-submitting", "");
                const btn = form.querySelector("button[type=submit]");
                btn.disabled = true;
                btn.setAttribute("data-tlm-submit-guard", "");
                window.dispatchEvent(
                    new PageTransitionEvent("pageshow", { persisted: true })
                );
            }"""
        )
        assert submit.is_enabled()
        assert (
            page.locator("form.render-trigger-panel[data-tlm-submitting]").count() == 0
        )


class TestBulkDeleteDoubleClick:
    def test_double_click_arms_but_does_not_fire(
        self, frames_env: tuple[Any, str, Path]
    ) -> None:
        """#99: the second click of a double click must not confirm the delete."""
        page, base, db_path = frames_env
        frame_id = _open_frames_and_select_first(page, base)
        posts = _record_posts(page, "/frames/bulk")
        delete_btn = page.locator('#frames-action-bar [data-bulk-action="delete"]')

        delete_btn.dblclick()
        page.wait_for_timeout(_HOLD_MS)
        assert posts == []
        assert "Confirm" in delete_btn.text_content()
        assert (
            _query(db_path, "SELECT lifecycle_state FROM frame WHERE id = ?", frame_id)[
                0
            ][0]
            == "active"
        )

        # A deliberate confirm after the arming delay still sends the delete.
        # (Its result announcement is covered by TestBulkResultAnnouncement.)
        page.wait_for_timeout(600)
        delete_btn.click()
        deadline = time.monotonic() + 10.0
        while not posts and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert len(posts) == 1


class TestBulkResultAnnouncement:
    @pytest.mark.parametrize("action", ["delete", "exclude"])
    def test_successful_bulk_action_keeps_and_announces_its_result(
        self, frames_env: tuple[Any, str, Path], action: str
    ) -> None:
        """#122: the out-of-band tile swap must not replace the result bar.

        It used to restore the selection bar before the result was processed,
        so a successful Delete announced "Action failed; nothing was changed."
        and an Exclude lost its result and Undo.
        """
        page, base, db_path = frames_env
        frame_id = _open_frames_and_select_first(page, base)
        button = page.locator(f'#frames-action-bar [data-bulk-action="{action}"]')

        if action == "delete":
            # Two-step confirm: arm, wait out the #99 arming delay, confirm.
            button.click()
            page.wait_for_timeout(600)
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/frames/bulk" in r.url,
            timeout=_TIMEOUT,
        ) as response_info:
            button.click()
        assert response_info.value.status == 200
        page.wait_for_function(
            "() => document.getElementById('frame-action-status').textContent.trim()",
            timeout=_TIMEOUT,
        )
        page.wait_for_timeout(_HOLD_MS)

        bar = page.locator("#frames-action-bar")
        assert "frames-action-bar--result" in (bar.get_attribute("class") or "")
        summary = (bar.locator(".selection-bar-count").text_content() or "").strip()
        status = (page.locator("#frame-action-status").text_content() or "").strip()
        assert status == summary, (status, summary)
        assert "failed" not in status.lower()
        assert page.locator("#frames-action-bar [data-bulk-undo]").count() == 1
        if action == "delete":
            state = _query(
                db_path, "SELECT lifecycle_state FROM frame WHERE id = ?", frame_id
            )
            assert state[0][0] == "soft_deleted"

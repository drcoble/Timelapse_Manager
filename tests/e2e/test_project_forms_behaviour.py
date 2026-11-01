"""The project and schedule forms behave without inline script (#70 WP-70-M1).

Their handlers were inline ``on*=`` attributes and ``<script>`` blocks; they are
now ``data-action`` controls driven by delegated listeners (actions.js and
project-forms.js). These tests exercise every converted control in the places the
forms really appear (the New Project drawer, the project Settings tab, the
milestone form), and do so *after a second htmx swap* of the form, which is what
shows that delegation survives swaps. Each flow must also leave the page free of
CSP violations and page errors.

Requires the ``ui`` marker. Uses ``logged_in_data_page`` (the seeded camera has a
location, so the solar anchor kinds are selectable).
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    SecurityEvents,
    _boot_app,
    _seed_demo_data,
    login,
    stop_app,
)

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_DRAWER_OPEN_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'false'"
)
_DRAWER_CLOSED_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'true'"
)
_ROWS = "#exact-time-rows .exact-time-row"

# True once htmx has processed the matching control. A click that lands before
# that is a plain click that sends no request, so the test would wait on a swap
# that never comes (#306; same pattern as #265 and #270).
_BOUND_JS = "(sel) => !!document.querySelector(sel)?.['htmx-internal-data']"


def _wait_htmx_bound(page: object, selector: str) -> None:
    page.wait_for_function(_BOUND_JS, arg=selector, timeout=_TIMEOUT)  # type: ignore[attr-defined]


def _seed_anchor_only_project(db_url: str, data_dir: Path) -> None:
    """The demo data plus project 2: anchor-only, as every production project is."""
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import Project
    from timelapse_manager.db.session import create_session_factory, session_scope

    _seed_demo_data(db_url, data_dir)
    engine = create_db_engine(db_url)
    try:
        with session_scope(create_session_factory(engine)) as db:
            db.add(
                Project(
                    camera_id=1,
                    name="Anchor Only",
                    capture_interval_seconds=None,
                    lifecycle_state="active",
                    exact_time_anchors=[
                        {"kind": "solar_noon", "offset_minutes": 0, "enabled": True},
                        {"kind": "clock", "time": "12:05", "enabled": True},
                    ],
                )
            )
    finally:
        engine.dispose()


@pytest.fixture()
def anchor_only_page(
    _chromium_page: object, tmp_path: Path
) -> Generator[tuple[object, str], None, None]:
    proc, base = _boot_app(tmp_path, extra_seed=_seed_anchor_only_project)
    try:
        login(_chromium_page, base)
        yield _chromium_page, base
    finally:
        stop_app(proc)


def _open_new_project_drawer(page: object) -> None:
    _wait_htmx_bound(page, 'a[hx-get="/drawers/new-project"]')
    page.locator('a[hx-get="/drawers/new-project"]').first.click()  # type: ignore[attr-defined]
    page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[attr-defined]
    page.wait_for_selector(  # type: ignore[attr-defined]
        "#drawer-main #capture_interval_value", timeout=_TIMEOUT
    )


def _open_drawer_after_a_swap(page: object, base: str) -> None:
    """Open the drawer, close it, and open it again: the form is swapped in twice."""
    page.goto(f"{base}/", wait_until="domcontentloaded")  # type: ignore[attr-defined]
    _open_new_project_drawer(page)
    page.locator("[data-drawer-close]").first.click()  # type: ignore[attr-defined]
    # The close button sits over the header's user menu, which opens on hover
    # and would then cover the opener; move the pointer off it.
    page.mouse.move(400, 500)  # type: ignore[attr-defined]
    page.wait_for_function(_DRAWER_CLOSED_JS, timeout=_TIMEOUT)  # type: ignore[attr-defined]
    # Let the slide-out transition finish: until it does the drawer's form is
    # still over the opener and would take the click.
    page.wait_for_function(  # type: ignore[attr-defined]
        "() => document.querySelector('#drawer-main').getBoundingClientRect().left"
        " >= window.innerWidth",
        timeout=_TIMEOUT,
    )
    _open_new_project_drawer(page)


def _hidden(page: object, selector: str) -> bool:
    return bool(page.locator(selector).first.evaluate("el => el.hidden"))  # type: ignore[attr-defined]


def _exercise_schedule_preset(page: object) -> None:
    sun, custom = "#sched-sun-panel", "#sched-custom-panel"
    for preset, sun_hidden, custom_hidden in (
        ("sun", False, True),
        ("custom", True, False),
        ("business", True, True),
        ("always", True, True),
    ):
        page.check(f"input[name='capture_schedule_preset'][value='{preset}']")  # type: ignore[attr-defined]
        assert _hidden(page, sun) is sun_hidden, preset
        assert _hidden(page, custom) is custom_hidden, preset


def _exercise_capture_mode(page: object) -> None:
    page.check("input[name='capture_mode'][value='interval']")  # type: ignore[attr-defined]
    assert page.locator("#capture_interval_value").is_enabled()  # type: ignore[attr-defined]
    assert page.locator(".interval-only-field").first.is_visible()  # type: ignore[attr-defined]

    # Solar mode hides the interval fields, disables the interval input (so it
    # neither validates nor submits) and seeds one solar-noon capture time.
    page.check("input[name='capture_mode'][value='solar']")  # type: ignore[attr-defined]
    assert page.locator("#capture_interval_value").is_disabled()  # type: ignore[attr-defined]
    assert not page.locator(".interval-only-field").first.is_visible()  # type: ignore[attr-defined]
    assert page.locator(_ROWS).count() == 1  # type: ignore[attr-defined]
    assert page.locator(f"{_ROWS} select[name='anchor_kind']").input_value() == (  # type: ignore[attr-defined]
        "solar_noon"
    )
    assert _hidden(page, f"{_ROWS} [data-time-group]")

    # Switching back keeps the seeded row and re-enables the interval.
    page.check("input[name='capture_mode'][value='interval']")  # type: ignore[attr-defined]
    assert page.locator("#capture_interval_value").is_enabled()  # type: ignore[attr-defined]
    assert page.locator(_ROWS).count() == 1  # type: ignore[attr-defined]


def _exercise_exact_time_rows(page: object) -> None:
    """Add, retype and remove capture times; new-row indices stay contiguous."""
    add = "button[data-action='exact-add']"
    existing = page.locator(_ROWS).count()  # type: ignore[attr-defined]
    page.click(add)  # type: ignore[attr-defined]
    page.click(add)  # type: ignore[attr-defined]
    assert page.locator(_ROWS).count() == existing + 2  # type: ignore[attr-defined]
    assert page.locator("#exact-time-empty-state").count() in (0, 1)  # type: ignore[attr-defined]

    new_boxes = "#exact-time-rows input[name='anchor_enabled'][value^='new:']"
    values = page.locator(new_boxes).evaluate_all("els => els.map(e => e.value)")  # type: ignore[attr-defined]
    assert values == [f"new:{i}" for i in range(len(values))]

    # Kind switch on the last row: a solar kind hides and clears the time; a
    # clock kind shows it again.
    last = page.locator(_ROWS).last  # type: ignore[attr-defined]
    time_input = last.locator("input[name='anchor_time']")
    last.locator("select[name='anchor_kind']").select_option("clock")
    time_input.fill("12:30")
    assert time_input.is_visible()
    last.locator("select[name='anchor_kind']").select_option("sunrise")
    assert not time_input.is_visible()
    assert time_input.input_value() == ""
    last.locator("select[name='anchor_kind']").select_option("clock")
    assert time_input.is_visible()

    # Remove the first of the two rows just added: the survivors renumber
    # contiguously, and the removal fires a bubbling change (the pre-flight
    # listens for it, since a removed input fires none of its own).
    before = page.locator(new_boxes).count()  # type: ignore[attr-defined]
    page.evaluate(  # type: ignore[attr-defined]
        "() => { window.__tlmChanged = 0;"
        " document.addEventListener('change', () => { window.__tlmChanged++; }); }"
    )
    page.locator(f"{_ROWS} button[data-action='exact-remove']").nth(existing).click()  # type: ignore[attr-defined]
    assert page.locator(_ROWS).count() == existing + 1  # type: ignore[attr-defined]
    survivors = page.locator(new_boxes).evaluate_all("els => els.map(e => e.value)")  # type: ignore[attr-defined]
    assert survivors == [f"new:{i}" for i in range(before - 1)]
    assert page.evaluate("() => window.__tlmChanged") >= 1  # type: ignore[attr-defined]

    # Remove everything that is left: the empty state returns.
    while page.locator(_ROWS).count():  # type: ignore[attr-defined]
        page.locator(f"{_ROWS} button[data-action='exact-remove']").first.click()  # type: ignore[attr-defined]
    assert page.locator("#exact-time-empty-state").is_visible()  # type: ignore[attr-defined]


def _assert_clean(events: SecurityEvents) -> None:
    assert events.csp_violations == []
    assert events.page_errors == []


class TestNewProjectDrawerForm:
    def test_controls_work_after_a_second_swap(
        self,
        logged_in_data_page: tuple[object, str],
        security_events: Callable[[object], SecurityEvents],
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        events = security_events(page)
        _open_drawer_after_a_swap(page, base)
        events.clear()

        _exercise_schedule_preset(page)
        _exercise_capture_mode(page)
        _exercise_exact_time_rows(page)
        _assert_clean(events)


class TestProjectSettingsForm:
    def _open_settings(self, page: object, base: str) -> None:
        page.goto(f"{base}/projects/1", wait_until="domcontentloaded")  # type: ignore[attr-defined]
        page.locator('[data-tab-target="#tab-settings"]').click()  # type: ignore[attr-defined]
        page.wait_for_selector(  # type: ignore[attr-defined]
            '#tab-settings form[action="/projects/1/settings"]', timeout=_TIMEOUT
        )

    def test_controls_work(
        self,
        logged_in_data_page: tuple[object, str],
        security_events: Callable[[object], SecurityEvents],
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        events = security_events(page)
        self._open_settings(page, base)
        events.clear()

        _exercise_schedule_preset(page)
        _exercise_capture_mode(page)
        _exercise_exact_time_rows(page)
        _assert_clean(events)

    def test_render_fps_chip_fills_the_input(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        self._open_settings(page, base)
        chips = page.locator("button[data-action='set-render-fps']")  # type: ignore[union-attr]
        if chips.count() == 0:
            pytest.skip("the seeded 300 s interval offers no frame-rate suggestions")
        # The frame-rate controls sit in a collapsed <details>.
        page.evaluate(  # type: ignore[union-attr]
            "() => document.querySelector('#render_fps').closest('details').open = true"
        )
        chip = chips.last
        expected = chip.get_attribute("data-fps")
        chip.click()
        assert page.input_value("#render_fps") == expected  # type: ignore[union-attr]


class TestAnchorOnlyProjectSettings:
    def test_solar_mode_is_applied_when_the_form_loads(
        self, anchor_only_page: tuple[object, str]
    ) -> None:
        """A saved anchor-only project opens with the interval fields off.

        Nothing is clicked: the capture mode is applied from the loaded
        fragment (htmx.onLoad), including the interval *unit* select, which
        the server does not render disabled.
        """
        page, base = anchor_only_page  # type: ignore[misc]
        page.goto(f"{base}/projects/2", wait_until="domcontentloaded")  # type: ignore[union-attr]
        page.locator('[data-tab-target="#tab-settings"]').click()  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            '#tab-settings form[action="/projects/2/settings"]', timeout=_TIMEOUT
        )
        assert page.locator("input[name='capture_mode']:checked").input_value() == (  # type: ignore[union-attr]
            "solar"
        )
        assert page.locator("#capture_interval_value").is_disabled()  # type: ignore[union-attr]
        # htmx runs onLoad once the swap has settled, a moment after the form
        # appears, so wait for it rather than sampling at once.
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.querySelector("
            "\"select[name='capture_interval_unit']\").disabled",
            timeout=_TIMEOUT,
        )
        assert not page.locator(".interval-only-field").first.is_visible()  # type: ignore[union-attr]
        assert page.locator(_ROWS).count() == 2  # type: ignore[union-attr]
        # An existing row's kind switch works too (the clock row, then back).
        clock = page.locator(f"{_ROWS} select[name='anchor_kind']").nth(1)  # type: ignore[union-attr]
        clock.select_option("sunset")
        assert page.locator(f"{_ROWS} [data-time-group]").nth(1).is_hidden()  # type: ignore[union-attr]


class TestMilestoneForm:
    def test_position_radio_reveals_the_matching_panel_after_two_swaps(
        self,
        logged_in_data_page: tuple[object, str],
        security_events: Callable[[object], SecurityEvents],
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        events = security_events(page)
        page.goto(f"{base}/projects/1", wait_until="domcontentloaded")  # type: ignore[union-attr]
        page.locator('[data-tab-target="#tab-milestones"]').click()  # type: ignore[union-attr]
        events.clear()

        add_selector = 'button[hx-get="/projects/1/milestones/new"]'
        add = page.locator(add_selector)  # type: ignore[union-attr]
        for _ in range(2):  # the second click swaps the form in again
            _wait_htmx_bound(page, add_selector)
            add.click()
            page.wait_for_selector("#ms-new-card", timeout=_TIMEOUT)  # type: ignore[union-attr]
        assert page.locator("#ms-new-card").count() == 1  # type: ignore[union-attr]

        page.check("#ms-new-type-time")  # type: ignore[union-attr]
        assert _hidden(page, "#ms-new-frame-panel")
        assert not _hidden(page, "#ms-new-time-panel")
        page.check("#ms-new-type-frame")  # type: ignore[union-attr]
        assert not _hidden(page, "#ms-new-frame-panel")
        assert _hidden(page, "#ms-new-time-panel")
        _assert_clean(events)


class TestEventTriggerRows:
    """The trigger rows need a camera that reports events; inject their markup."""

    _FIXTURE = """
    () => {
      document.body.insertAdjacentHTML('beforeend', `
        <form id="trig-fixture">
          <div id="event-trigger-rows">
            <div class="form-row event-trigger-row">
              <input type="checkbox" name="trigger_enabled" value="7" checked>
              <button type="button" data-action="trigger-remove">x</button>
            </div>
          </div>
          <p id="event-trigger-empty-state" hidden>none</p>
          <button type="button" id="trig-add" data-action="trigger-add">add</button>
          <template id="event-trigger-row-template">
            <div class="form-row event-trigger-row">
              <div class="form-group">
                <label class="form-label">Event</label>
                <select name="trigger_topic"><option value="">-</option></select>
              </div>
              <div class="form-group">
                <label class="form-label">Cooldown</label>
                <input type="number" name="trigger_cooldown" value="10">
              </div>
              <input type="checkbox" name="trigger_enabled" value="" checked>
              <button type="button" data-action="trigger-remove">x</button>
            </div>
          </template>
        </form>`);
    }
    """

    def test_add_stamps_new_indices_and_remove_renumbers(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/about", wait_until="domcontentloaded")  # type: ignore[union-attr]
        page.evaluate(self._FIXTURE)  # type: ignore[union-attr]

        boxes = "#event-trigger-rows input[name='trigger_enabled']"

        def values() -> list[str]:
            return page.locator(boxes).evaluate_all(  # type: ignore[union-attr, no-any-return]
                "els => els.map(e => e.value)"
            )

        page.click("#trig-add")  # type: ignore[union-attr]
        page.click("#trig-add")  # type: ignore[union-attr]
        # Indexed among new rows only: the saved row (7) does not count.
        assert values() == ["7", "new:0", "new:1"]
        # Each cloned label points at its control's fresh id.
        for_ids = page.locator("#event-trigger-rows label.form-label").evaluate_all(  # type: ignore[union-attr]
            "els => els.map(l => [l.htmlFor, !!document.getElementById(l.htmlFor)])"
        )
        assert for_ids and all(found for _, found in for_ids)

        page.locator(  # type: ignore[union-attr]
            "#event-trigger-rows .event-trigger-row button"
        ).nth(1).click()
        assert values() == ["7", "new:0"]

        while page.locator("#event-trigger-rows .event-trigger-row").count():  # type: ignore[union-attr]
            page.locator("#event-trigger-rows .event-trigger-row button").first.click()  # type: ignore[union-attr]
        assert page.locator("#event-trigger-empty-state").is_visible()  # type: ignore[union-attr]

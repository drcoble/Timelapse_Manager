"""E2E for the render-trigger panel: open, fps chip sets value, combo-check.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses the seeded ``logged_in_data_page`` (Project id 1).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui


class TestRenderTrigger:
    def test_panel_opens_and_fps_chip(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        # Renders tab → open the trigger panel.
        page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[union-attr]
        page.click(".render-trigger-details > summary")  # type: ignore[union-attr]
        page.wait_for_selector("#trigger-render-fps", timeout=30000)  # type: ignore[union-attr]
        # An fps suggestion chip writes the value into the fps input.
        page.click(".render-trigger-panel .chip-group .chip >> nth=0")  # type: ignore[union-attr]
        assert page.input_value("#trigger-render-fps") != ""  # type: ignore[union-attr]

    def test_combo_check_warns_on_bad_pair(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[union-attr]
        page.click(".render-trigger-details > summary")  # type: ignore[union-attr]
        page.wait_for_selector("#trigger-render-encoder", timeout=30000)  # type: ignore[union-attr]
        # The Settings tab's render form loads into this page with its own
        # render_* selects; let it settle (its encoder fires a combo-check on
        # load) so the race this test once lost is in play every run (#34).
        page.wait_for_selector("#render_encoder", state="attached", timeout=30000)  # type: ignore[union-attr]
        # VP9 + MP4 is not muxable -> combo-check warns.
        page.select_option("#trigger-render-encoder", "libvpx-vp9")  # type: ignore[union-attr]
        with page.expect_response(  # type: ignore[union-attr]
            lambda r: (
                "/renders/combo-check" in r.url
                and "render_container=mp4" in r.url
                and "render_encoder=libvpx-vp9" in r.url
            )
        ) as info:
            page.select_option("#trigger-render-container", "mp4")  # type: ignore[union-attr]
        # The request carries only this panel's two selects.
        assert info.value.url.count("render_encoder=") == 1  # type: ignore[union-attr]
        # The warning (not the "plays only in ..." notice) lands in this panel.
        page.wait_for_selector(  # type: ignore[union-attr]
            "#trigger-render-combo-warning .alert.warning", timeout=30000
        )

    def test_double_submit_queues_one_render(
        self, logged_in_data_page: tuple[object, str], tmp_path: Path
    ) -> None:
        """#67 R14: two concurrent submits of the panel's form queue one job.

        The browser posts the real form (its CSRF token and session cookie) twice
        at once, as a double-click does; the server keeps one ``pending`` job.
        The e2e server runs no render worker, so the job stays ``pending``.
        """
        page, base = logged_in_data_page  # type: ignore[misc]
        before = _manual_jobs(tmp_path)
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[union-attr]
        page.click(".render-trigger-details > summary")  # type: ignore[union-attr]
        page.wait_for_selector("form.render-trigger-panel", timeout=30000)  # type: ignore[union-attr]
        statuses = page.evaluate(  # type: ignore[union-attr]
            """async () => {
                const form = document.querySelector("form.render-trigger-panel");
                const post = () => fetch(form.action, {
                    method: "POST", body: new FormData(form), redirect: "manual",
                });
                const responses = await Promise.all([post(), post()]);
                return responses.map(
                    (r) => r.type === "opaqueredirect" ? 303 : r.status
                );
            }"""
        )
        assert sorted(statuses) == [200, 303]
        assert len(_manual_jobs(tmp_path)) == len(before) + 1


def _manual_jobs(tmp_path: Path) -> list[int]:
    """Ids of the manual render jobs in the live server's database."""
    with closing(sqlite3.connect(tmp_path / "e2e_test.db")) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT id FROM render_job WHERE kind = 'manual' ORDER BY id"
            )
        ]

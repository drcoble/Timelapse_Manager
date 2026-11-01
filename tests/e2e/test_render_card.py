"""E2E for the render card and export card (issue #9).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.

The server is seeded with one project, two finished renders (each with a real
output file in the project's render root) and one finished frame export, so the
cards show file sizes and the delete removes real files. Covers:

* opening the same card from the project Renders tab and from /renders;
* the card surviving the Renders page's list refresh (forced here instead of
  waiting 10 s), and focus returning to the re-rendered row on Escape;
* deleting through the in-card confirmation: the dialog closes, the row
  disappears, and a toast says "Render #N deleted";
* the export card on the project Frames tab.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from tests.e2e.conftest import _boot_app, login, stop_app

pytestmark = pytest.mark.ui


def _seed_renders(db_url: str, data_dir: Path) -> None:
    """Seed project 1 with renders 1 and 2 (files on disk) and export 3."""
    from datetime import datetime

    from timelapse_manager.config.settings import (
        DatabaseSettings,
        PathsSettings,
        SecretsSettings,
        Settings,
    )
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import Camera, Project, RenderJob
    from timelapse_manager.db.session import create_session_factory, session_scope
    from timelapse_manager.render.spec import project_render_root

    settings = Settings(
        database=DatabaseSettings(url=db_url),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        secrets=SecretsSettings(use_os_keystore=False),
    )
    engine = create_db_engine(db_url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as db:
            cam = Camera(name="card-cam", address="10.0.0.31", protocol="vapix")
            db.add(cam)
            db.flush()
            proj = Project(
                camera_id=cam.id,
                name="Card Project",
                capture_interval_seconds=300,
                lifecycle_state="active",
            )
            db.add(proj)
            db.flush()
            root = project_render_root(settings, proj)
            root.mkdir(parents=True, exist_ok=True)
            for kind, name in (
                ("manual", "render-1.mp4"),
                ("manual", "render-2.mp4"),
                ("export", "export-3.zip"),
            ):
                path = root / name
                path.write_bytes(b"\x00" * 4096)
                db.add(
                    RenderJob(
                        project_id=proj.id,
                        kind=kind,
                        status="done",
                        output_settings={
                            "fps": 24,
                            "width": 1280,
                            "height": 720,
                            "codec": "h264",
                            "container": "mp4",
                        },
                        output_file_path=str(path),
                        browser_streamable=True,
                        started_at=datetime(2026, 10, 1, 12, 0),
                        completed_at=datetime(2026, 10, 1, 12, 2),
                        frame_count=240,
                        first_frame_at=datetime(2026, 9, 1, 6, 0),
                        last_frame_at=datetime(2026, 9, 30, 18, 0),
                    )
                )
                db.flush()
    finally:
        engine.dispose()


@pytest.fixture()
def card_page(
    _chromium_page: object, tmp_path: Path
) -> Generator[tuple[object, str, Path], None, None]:
    """An authenticated page on a server seeded with renders and an export.

    Yields (page, base_url, render_root).
    """
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_renders)
    try:
        login(_chromium_page, base_url)
        root = tmp_path / "data" / "frames"
        yield _chromium_page, base_url, root
    finally:
        stop_app(proc)


def _open_card(page: object, opener: str) -> None:
    page.click(opener)  # type: ignore[attr-defined]
    page.wait_for_selector(  # type: ignore[attr-defined]
        "#job-card-modal[aria-hidden='false'] .job-card", timeout=30000
    )


class TestRenderCard:
    def test_same_card_from_project_tab_and_renders_page(
        self, card_page: tuple[object, str, Path]
    ) -> None:
        page, base, _ = card_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[union-attr]
        _open_card(page, "#render-open-2")
        from_project = page.inner_text("#job-card-modal .job-card")  # type: ignore[union-attr]
        assert "Card Project" in from_project
        assert "render-2.mp4" in from_project
        assert "240 frames" in from_project
        page.keyboard.press("Escape")  # type: ignore[union-attr]

        page.goto(f"{base}/renders")  # type: ignore[union-attr]
        # The export never appears on the render list.
        assert page.locator("#render-row-3").count() == 0  # type: ignore[union-attr]
        _open_card(page, "#render-open-2")
        from_renders = page.inner_text("#job-card-modal .job-card")  # type: ignore[union-attr]
        assert from_renders == from_project

    def test_card_survives_list_refresh_and_escape_returns_focus(
        self, card_page: tuple[object, str, Path]
    ) -> None:
        page, base, _ = card_page  # type: ignore[misc]
        page.goto(f"{base}/renders")  # type: ignore[union-attr]
        _open_card(page, "#render-open-1")
        # Force the 10 s poll now: replace #renders-list exactly as it does.
        page.evaluate(  # type: ignore[union-attr]
            """() => new Promise((resolve) => {
                document.body.addEventListener('htmx:afterSettle', resolve,
                                               {once: true});
                htmx.ajax('GET', '/renders', {target: '#renders-list',
                          swap: 'outerHTML', select: '#renders-list'});
            })"""
        )
        # The refresh replaced only the list (no page nested inside it).
        assert page.locator("#renders-list").count() == 1  # type: ignore[union-attr]
        assert page.locator("#job-card-modal").count() == 1  # type: ignore[union-attr]
        assert page.is_visible("#job-card-modal .job-card")  # type: ignore[union-attr]
        assert "Render #1" in page.inner_text("#job-card-title")  # type: ignore[union-attr]

        page.keyboard.press("Escape")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#job-card-modal[aria-hidden='true']", state="attached"
        )
        focused = page.evaluate("document.activeElement && document.activeElement.id")  # type: ignore[union-attr]
        assert focused == "render-open-1"

    def test_focus_is_trapped_in_the_card(
        self, card_page: tuple[object, str, Path]
    ) -> None:
        page, base, _ = card_page  # type: ignore[misc]
        page.goto(f"{base}/renders")  # type: ignore[union-attr]
        _open_card(page, "#render-open-1")
        for _ in range(12):
            page.keyboard.press("Tab")  # type: ignore[union-attr]
            inside = page.evaluate(  # type: ignore[union-attr]
                "document.getElementById('job-card-modal')"
                ".contains(document.activeElement)"
            )
            assert inside

    def test_delete_with_confirmation_removes_row_and_file(
        self, card_page: tuple[object, str, Path]
    ) -> None:
        page, base, frames_root = card_page  # type: ignore[misc]
        files = list(frames_root.rglob("render-2.mp4"))
        assert len(files) == 1
        page.goto(f"{base}/renders")  # type: ignore[union-attr]
        _open_card(page, "#render-open-2")

        page.click("#job-card-modal .job-card-delete > summary")  # type: ignore[union-attr]
        confirm = page.inner_text("#job-card-modal .inline-confirm")  # type: ignore[union-attr]
        assert "render-2.mp4" in confirm and "4.0 KB" in confirm
        page.click("#job-card-modal .inline-confirm button[type='submit']")  # type: ignore[union-attr]

        page.wait_for_selector(  # type: ignore[union-attr]
            "#render-row-2", state="detached", timeout=30000
        )
        page.wait_for_selector(  # type: ignore[union-attr]
            "#toast-region .toast:has-text('Render #2 deleted')", timeout=30000
        )
        assert page.get_attribute("#job-card-modal", "aria-hidden") == "true"  # type: ignore[union-attr]
        assert page.locator("#render-row-1").count() == 1  # type: ignore[union-attr]
        assert not files[0].exists()

        # The project's Renders tab no longer lists it either.
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        assert page.locator("#render-row-2").count() == 0  # type: ignore[union-attr]

    def test_list_stays_refreshable_after_the_last_render_is_deleted(
        self, card_page: tuple[object, str, Path]
    ) -> None:
        page, base, _ = card_page  # type: ignore[misc]
        page.goto(f"{base}/renders")  # type: ignore[union-attr]
        for render_id in (2, 1):
            _open_card(page, f"#render-open-{render_id}")
            page.click("#job-card-modal .job-card-delete > summary")  # type: ignore[union-attr]
            page.click("#job-card-modal .inline-confirm button[type='submit']")  # type: ignore[union-attr]
            page.wait_for_selector(  # type: ignore[union-attr]
                f"#render-row-{render_id}", state="detached", timeout=30000
            )
            page.wait_for_selector(  # type: ignore[union-attr]
                "#job-card-modal[aria-hidden='true']", state="attached"
            )

        # Refresh now, exactly as the tab coming back (tlm:visible) would.
        with page.expect_response(  # type: ignore[union-attr]
            lambda r: "/partials/renders/list" in r.url
        ) as info:
            page.evaluate("document.dispatchEvent(new Event('tlm:visible'))")  # type: ignore[union-attr]
        # Nothing is active: htmx is told to stop polling (#67 P5).
        assert info.value.status == 286
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.querySelector('#renders-list .empty-state') !== null"
        )
        assert page.locator("#renders-list").count() == 1  # type: ignore[union-attr]
        # The refresh trigger and passive marker survive the swap (WP-70-E1);
        # with nothing active there is no `every` poll.
        assert (
            page.get_attribute("#renders-list", "hx-trigger")  # type: ignore[union-attr]
            == "tlm:visible from:document"
        )
        assert page.get_attribute("#renders-list", "data-poll") is not None  # type: ignore[union-attr]
        assert "No renders yet" in page.inner_text("#renders-list")  # type: ignore[union-attr]


class TestExportCard:
    def test_export_card_from_frames_tab_and_delete(
        self, card_page: tuple[object, str, Path]
    ) -> None:
        page, base, frames_root = card_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-frames"]')  # type: ignore[union-attr]
        _open_card(page, "#export-open-3")
        text = page.inner_text("#job-card-modal .job-card")  # type: ignore[union-attr]
        assert "export-3.zip" in text
        assert "240 frames in the zip" in text

        page.click("#job-card-modal .job-card-delete > summary")  # type: ignore[union-attr]
        page.click("#job-card-modal .inline-confirm button[type='submit']")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#export-row-3", state="detached", timeout=30000
        )
        page.wait_for_selector(  # type: ignore[union-attr]
            "#toast-region .toast:has-text('Export #3 deleted')", timeout=30000
        )
        assert not list(frames_root.rglob("export-3.zip"))

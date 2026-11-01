"""E2E for playing the latest render from the Latest Frame (#24, #25).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.

The codec answer of the test browser is not the point here (headless Chromium
builds differ in proprietary codecs), so ``window.tlmPlayability.check`` is
stubbed to "plays" or "can't play" and the tests pin the page wiring: the
swap to an inline ``<video>``, Close and Space, and the can't-play note.
One test runs the real check on a MIME type no browser plays.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from tests.e2e.conftest import _boot_app, login, stop_app
from tests.e2e.test_render_card import _seed_renders

pytestmark = pytest.mark.ui


@pytest.fixture()
def play_page(
    _chromium_page: object, tmp_path: Path
) -> Generator[tuple[object, str], None, None]:
    """An authenticated page on a server whose project 1 has finished renders."""
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_renders)
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url
    finally:
        stop_app(proc)


def _stub_check(page: object, answer: bool) -> None:
    page.evaluate(  # type: ignore[attr-defined]
        "ok => { window.tlmPlayability.check = () => Promise.resolve(ok); }",
        answer,
    )


class TestLatestFramePlay:
    def test_click_plays_inline_and_close_restores_the_frame(
        self, play_page: tuple[object, str]
    ) -> None:
        page, base = play_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        _stub_check(page, True)

        page.click("a.latest-frame-play")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#latest-render-player:not([hidden]) video", timeout=10000
        )
        video = page.eval_on_selector(  # type: ignore[union-attr]
            "#latest-render-player video",
            "v => ({src: v.getAttribute('src'), muted: v.muted, "
            "controls: v.controls, inline: v.hasAttribute('playsinline')})",
        )
        assert video["src"] == "/renders/2/stream"
        assert video["muted"] and video["controls"] and video["inline"]
        assert page.is_hidden("#latest-frame-container")  # type: ignore[union-attr]
        assert page.url.endswith("/projects/1")  # type: ignore[union-attr]

        page.click("[data-latest-render-close]")  # type: ignore[union-attr]
        assert page.is_hidden("#latest-render-player")  # type: ignore[union-attr]
        assert page.is_visible("#latest-frame-container")  # type: ignore[union-attr]
        assert page.query_selector("#latest-render-player video") is None  # type: ignore[union-attr]

    def test_space_key_plays(self, play_page: tuple[object, str]) -> None:
        page, base = play_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        _stub_check(page, True)

        page.focus("a.latest-frame-play")  # type: ignore[union-attr]
        page.keyboard.press(" ")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#latest-render-player:not([hidden]) video", timeout=10000
        )

    def test_cant_play_shows_note_and_keeps_the_frame(
        self, play_page: tuple[object, str]
    ) -> None:
        page, base = play_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        _stub_check(page, False)

        page.click("a.latest-frame-play")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "[data-latest-render-note]:not([hidden])", timeout=10000
        )
        assert page.is_hidden("#latest-render-player")  # type: ignore[union-attr]
        assert page.is_visible("#latest-frame-container")  # type: ignore[union-attr]
        note = page.inner_text("[data-latest-render-note]")  # type: ignore[union-attr]
        # The note names codec and container (#36): this render is H.264/MP4.
        assert "can't play H.264 in MP4;" in note

    def test_real_check_says_no_for_an_unplayable_type(
        self, play_page: tuple[object, str]
    ) -> None:
        page, base = play_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        ok = page.evaluate(  # type: ignore[union-attr]
            """() => {
                const el = document.createElement('div');
                el.setAttribute('data-mime', 'video/x-tlm-none');
                el.setAttribute('data-codec', 'zz01.0');
                return window.tlmPlayability.check(el);
            }"""
        )
        assert ok is False

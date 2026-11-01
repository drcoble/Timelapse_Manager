"""The "Use default credentials" checkbox in a real browser (#318).

Runs against a server seeded with default credentials, so the checkbox is
rendered. Without JS the markup shows username/password; the creds-inherit
action hides and disables them while the box is ticked, on load and on toggle.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from tests.e2e.conftest import _boot_app, login, stop_app

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


def _seed_defaults(db_url: str, data_dir: Path) -> None:
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import CameraDefaultCredentials
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(db_url)
    try:
        with session_scope(create_session_factory(engine)) as db:
            db.add(CameraDefaultCredentials(id=1, enabled=True, username="shared-user"))
    finally:
        engine.dispose()


@pytest.fixture()
def live_server_defaults(tmp_path: Path) -> Generator[str, None, None]:
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_defaults)
    try:
        yield base_url
    finally:
        stop_app(proc)


def _open_add(page: object, base: str) -> None:
    page.goto(f"{base}/cameras", wait_until="networkidle")  # type: ignore[attr-defined]
    page.locator("[hx-get='/cameras/add-form']").first.click()  # type: ignore[attr-defined]
    page.locator("#camera-form-card").wait_for(timeout=_TIMEOUT)  # type: ignore[attr-defined]


def test_inherit_checkbox_hides_and_shows_credentials(
    _chromium_page: object, live_server_defaults: str
) -> None:
    page = _chromium_page
    login(page, live_server_defaults)
    _open_add(page, live_server_defaults)

    inherit = page.locator("[name=credentials_inherit_default]")  # type: ignore[attr-defined]
    creds = page.locator("#cam-new-creds")  # type: ignore[attr-defined]
    username = page.locator("#cam-new-username")  # type: ignore[attr-defined]

    # Ticked by default on Add: credentials hidden and disabled from the start
    # (htmx settles the swapped-in form a moment after the card appears).
    assert inherit.is_checked()
    creds.wait_for(state="hidden", timeout=_TIMEOUT)
    assert creds.is_hidden()
    assert username.is_disabled()

    inherit.click()
    assert creds.is_visible()
    assert username.is_enabled()

    inherit.click()
    assert creds.is_hidden()
    assert username.is_disabled()


def test_field_order_on_screen(
    _chromium_page: object, live_server_defaults: str
) -> None:
    page = _chromium_page
    login(page, live_server_defaults)
    _open_add(page, live_server_defaults)
    page.locator("[name=credentials_inherit_default]").click()  # type: ignore[attr-defined]
    page.locator("#cam-new-creds").wait_for(state="visible", timeout=_TIMEOUT)  # type: ignore[attr-defined]
    ys = []
    for selector in (
        "[name=address]",
        "[name=credentials_inherit_default]",
        "[name=username]",
        "[name=allow_basic_over_http]",
        "button:has-text('Query camera')",
        "#camera_name",
        "[name=device_hostname]",
        "[name=protocol]",
    ):
        box = page.locator(f"#camera-form-card {selector}").first.bounding_box()  # type: ignore[attr-defined]
        assert box is not None, selector
        ys.append(box["y"])
    assert ys == sorted(ys)
    # Name and hostname share a row; everything else stacks strictly.
    assert ys[5] <= ys[6] <= ys[7]

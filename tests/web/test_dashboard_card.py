"""Web tests for the clickable dashboard project card (#23).

The card uses the "stretched link" pattern: the project-name link is the card's
only link to ``/projects/{id}`` and its CSS ``::after`` covers the whole card.
These tests pin the markup that pattern depends on -- one overlay link per card,
action forms never nested inside a link, no separate View button, and a footer
that is omitted rather than left empty -- on every path that renders the card:
the full dashboard and both HTMX partial endpoints the 30 s poll uses.

Real click behaviour (which element receives the click) is covered by the E2E
suite in ``tests/e2e/test_dashboard_card.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.dependencies import templates

# Statuses the card branches on, with the actions an operator should see.
_ACTIONS_BY_STATUS: dict[str, list[str]] = {
    "running": ["pause", "stop"],
    "paused": ["resume", "stop"],
    "lowdisk": ["resume", "stop"],
    "error": ["stop"],
    "stopped": ["start"],
}


@dataclass
class _Card:
    """Structure of one rendered ``<article class="project-card">``."""

    card_id: str
    project_links: list[dict[str, str]] = field(default_factory=list)
    form_actions: list[str] = field(default_factory=list)
    forms_inside_link: int = 0
    has_footer: bool = False
    overdue_badges: int = 0
    text: str = ""


class _CardParser(HTMLParser):
    """Collect per-card structure from rendered dashboard HTML.

    Uses the stdlib parser so the assertions follow real element nesting rather
    than regexes over the source text.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[_Card] = []
        self._card: _Card | None = None
        self._depth = 0  # element depth inside the current card
        self._open_links = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: v or "" for k, v in attrs}
        classes = attr.get("class", "").split()
        if tag == "article" and "project-card" in classes:
            self._card = _Card(card_id=attr.get("id", ""))
            self.cards.append(self._card)
            self._depth = 0
            self._open_links = 0
        if self._card is None:
            return
        if tag not in _VOID_TAGS:
            self._depth += 1
        if tag == "a":
            self._open_links += 1
            if attr.get("href", "").startswith("/projects/"):
                self._card.project_links.append(attr)
        elif tag == "form":
            self._card.form_actions.append(attr.get("action", ""))
            if self._open_links:
                self._card.forms_inside_link += 1
        if "project-card-footer" in classes:
            self._card.has_footer = True
        if "project-card-overdue" in classes:
            self._card.overdue_badges += 1

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if tag == "a" and self._open_links:
            self._open_links -= 1
        if tag not in _VOID_TAGS:
            self._depth -= 1
        if tag == "article" and self._depth <= 0:
            self._card = None

    def handle_data(self, data: str) -> None:
        if self._card is not None:
            self._card.text += data


_VOID_TAGS = frozenset({"img", "input", "br", "hr", "meta", "link", "source"})


def _cards(html: str) -> list[_Card]:
    parser = _CardParser()
    parser.feed(html)
    parser.close()
    return parser.cards


def _card_for(html: str, project_id: int) -> _Card:
    matches = [c for c in _cards(html) if c.card_id == f"project-card-{project_id}"]
    assert len(matches) == 1, f"expected one card for project {project_id}"
    return matches[0]


def _assert_stretched_link(card: _Card, project_id: int, name: str) -> None:
    """The invariants the stretched-link overlay depends on."""
    assert len(card.project_links) == 1, card.project_links
    link = card.project_links[0]
    assert link["href"] == f"/projects/{project_id}"
    assert "project-card-link" in link.get("class", "").split()
    assert link.get("aria-label") == f"Open project {name}"
    # A focusable link removed from the tab order would add a dead stop or hide
    # the card from keyboard users; the overlay link is the one tab stop.
    assert "tabindex" not in link
    assert card.forms_inside_link == 0
    assert "View" not in card.text.split()


def _seed_project(name: str, *, lifecycle_state: str = "active") -> int:
    """Insert a camera and a project; returns the project id.

    With no live supervisor state an active project derives ``stopped``, so an
    operator's card carries a single Start form.
    """
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state=lifecycle_state,
            operational_status="idle",
            storage_path=f"/tmp/{name}",
        )
        db.add(proj)
        db.flush()
        return proj.id


# ---------------------------------------------------------------------------
# HTTP: every path that renders the card produces the same structure.
# ---------------------------------------------------------------------------

_CARD_PATHS = ("/", "/partials/projects", "/partials/projects/{id}/status")


class TestCardMarkupAcrossRenderPaths:
    @pytest.mark.parametrize("path", _CARD_PATHS)
    def test_operator_card_has_overlay_link_and_unnested_action(
        self, operator_client: TestClient, path: str
    ) -> None:
        project_id = _seed_project("tile-op")
        resp = operator_client.get(path.format(id=project_id))
        assert resp.status_code == 200
        card = _card_for(resp.text, project_id)
        _assert_stretched_link(card, project_id, "tile-op")
        assert card.form_actions == [f"/projects/{project_id}/start"]
        assert card.has_footer
        assert ">View<" not in resp.text

    @pytest.mark.parametrize("path", _CARD_PATHS)
    def test_viewer_card_has_no_forms_and_no_footer(
        self, viewer_client: TestClient, path: str
    ) -> None:
        project_id = _seed_project("tile-viewer")
        resp = viewer_client.get(path.format(id=project_id))
        assert resp.status_code == 200
        card = _card_for(resp.text, project_id)
        _assert_stretched_link(card, project_id, "tile-viewer")
        assert card.form_actions == []
        assert not card.has_footer

    def test_every_dashboard_card_has_exactly_one_overlay_link(
        self, operator_client: TestClient
    ) -> None:
        ids = [_seed_project(f"tile-many-{n}") for n in range(3)]
        for path in ("/", "/partials/projects"):
            resp = operator_client.get(path)
            assert resp.status_code == 200
            cards = _cards(resp.text)
            assert sorted(c.card_id for c in cards) == sorted(
                f"project-card-{i}" for i in ids
            )
            for card in cards:
                assert len(card.project_links) == 1
                assert card.forms_inside_link == 0

    def test_poll_partials_match_dashboard_card_structure(
        self, operator_client: TestClient
    ) -> None:
        # The 30 s swap must not change what the card is: same link, same
        # forms, same footer as the initial page render.
        project_id = _seed_project("tile-swap")
        renders = [
            _card_for(operator_client.get(p.format(id=project_id)).text, project_id)
            for p in _CARD_PATHS
        ]
        first = renders[0]
        for other in renders[1:]:
            assert other.project_links == first.project_links
            assert other.form_actions == first.form_actions
            assert other.has_footer == first.has_footer

    def test_archived_card_keeps_overlay_link_without_footer(
        self, operator_client: TestClient
    ) -> None:
        project_id = _seed_project("tile-archived", lifecycle_state="archived")
        # Archived cards appear in the dashboard's collapsed section (the poll
        # grid is active-only) and on the per-card status partial.
        for path in ("/", f"/partials/projects/{project_id}/status"):
            resp = operator_client.get(path)
            assert resp.status_code == 200
            card = _card_for(resp.text, project_id)
            _assert_stretched_link(card, project_id, "tile-archived")
            assert card.form_actions == []
            assert not card.has_footer

    def test_project_name_is_escaped_in_aria_label(
        self, operator_client: TestClient
    ) -> None:
        name = 'Roof "east" <cam>'
        project_id = _seed_project(name)
        for path in _CARD_PATHS:
            resp = operator_client.get(path.format(id=project_id))
            assert resp.status_code == 200
            # Autoescaped on both the TemplateResponse and the direct
            # env.render() path: no raw quote breaks out of the attribute.
            assert "<cam>" not in resp.text
            assert 'Roof "east"' not in resp.text
            # The parser decodes entities, so the label round-trips intact.
            _assert_stretched_link(_card_for(resp.text, project_id), project_id, name)


# ---------------------------------------------------------------------------
# Template: every operational status (the HTTP tests can only reach the
# supervisor-less ``stopped`` / lifecycle ``paused`` states).
# ---------------------------------------------------------------------------


def _render_card(**overrides: Any) -> str:
    project: dict[str, Any] = {
        "id": 7,
        "name": "Card Seven",
        "operational_status": "running",
        "lifecycle_state": "active",
        "frame_count": 3,
        "latest_frame_url": "/frames/7/latest.jpg",
        "is_overdue": False,
    }
    project.update(overrides)
    template = templates.env.get_template("_partials/project_status_card.html")
    return template.render(
        project=SimpleNamespace(**project), can_operate=True, csrf_token="tok"
    )


class TestCardTemplateByStatus:
    @pytest.mark.parametrize("status", sorted(_ACTIONS_BY_STATUS))
    def test_actions_sit_outside_the_overlay_link(self, status: str) -> None:
        card = _card_for(_render_card(operational_status=status), 7)
        _assert_stretched_link(card, 7, "Card Seven")
        assert card.form_actions == [
            f"/projects/7/{action}" for action in _ACTIONS_BY_STATUS[status]
        ]
        assert card.has_footer

    def test_thumbnail_is_not_a_separate_link(self) -> None:
        html = _render_card()
        assert 'src="/frames/7/latest.jpg"' in html
        assert 'tabindex="-1"' not in html
        assert len(_card_for(html, 7).project_links) == 1

    def test_overdue_badge_is_marked_for_raising_above_overlay(self) -> None:
        card = _card_for(_render_card(is_overdue=True), 7)
        assert card.overdue_badges == 1

    def test_unprivileged_render_omits_footer(self) -> None:
        template = templates.env.get_template("_partials/project_status_card.html")
        html = template.render(
            project=SimpleNamespace(
                id=7,
                name="Card Seven",
                operational_status="running",
                lifecycle_state="active",
                latest_frame_url=None,
            ),
            can_operate=False,
            csrf_token="tok",
        )
        card = _card_for(html, 7)
        _assert_stretched_link(card, 7, "Card Seven")
        assert card.form_actions == []
        assert not card.has_footer

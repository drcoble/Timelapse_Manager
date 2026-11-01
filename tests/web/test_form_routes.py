"""Every template ``<form>`` posts to a route that exists (#70 F-R9).

A form's plain ``action`` is what the browser uses without JavaScript (and what
htmx falls back to for a form with no ``hx-*`` verb). The camera row's Delete
form had ``action="/cameras/{id}"`` while the only route there is ``DELETE``,
so a no-JS delete ended on a raw 405 body; nothing noticed because every test
drove the htmx verb. This test scans every template for ``<form>`` tags, turns
each ``action`` into a concrete path, and asserts that the app has a route for
that path **and** the form's method.

How an action becomes a path:
- ``{# … #}`` comments are dropped first;
- ``{% if … %}A{% else %}B{% endif %}`` yields both ``A`` and ``B``;
- a ``{{ … }}`` that fills a whole path segment becomes ``1``; one glued to
  other text (``/restore{{ ap_q }}``, a query-string suffix) is dropped;
- the query string is dropped.

An action that is a single expression (``{{ card.cancel_url }}``) cannot be
resolved from the template; those are listed in ``_DYNAMIC_ACTIONS`` so a new
one is a conscious addition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.routing import Match

import timelapse_manager.web as web_pkg

_TEMPLATES = Path(web_pkg.__file__).resolve().parent / "templates"

# (template, raw action) pairs whose URL is computed by the view model.
_DYNAMIC_ACTIONS = frozenset(
    {
        ("_partials/inline_confirm.html", "{{ confirm_action }}"),
        ("_partials/job_card_delete.html", "{{ card.delete_url }}"),
        ("_partials/render_card.html", "{{ card.cancel_url }}"),
    }
)

_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_IF_ELSE = re.compile(
    r"\{%-?\s*if\b[^%]*%\}(.*?)\{%-?\s*else\s*-?%\}(.*?)\{%-?\s*endif\s*-?%\}", re.S
)
_SEGMENT_EXPR = re.compile(r"(?<=/)\{\{.*?\}\}(?=/|\?|$)", re.S)
_ANY_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
_ANY_STMT = re.compile(r"\{%.*?%\}", re.S)


@dataclass(frozen=True)
class FormTag:
    """One ``<form>`` start tag found in a template."""

    template: str
    method: str
    action: str


class _FormCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "form":
            return
        values = dict(attrs)
        self.forms.append(
            ((values.get("method") or "get").lower(), values.get("action"))
        )


def _mask_jinja(text: str) -> tuple[str, dict[str, str]]:
    """Hide Jinja blocks from the HTML parser; return the text and the map back.

    Jinja inside a tag (``{% if %}`` in an attribute value, ``>`` in an
    expression) would otherwise confuse the parser.
    """
    blocks: dict[str, str] = {}

    def keep(match: re.Match[str]) -> str:
        key = f"@@jinja{len(blocks)}@@"
        blocks[key] = match.group(0)
        return key

    masked = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", keep, _COMMENT.sub("", text), flags=re.S)
    return masked, blocks


def _unmask(value: str, blocks: dict[str, str]) -> str:
    for key, original in blocks.items():
        value = value.replace(key, original)
    return value


def _collect_forms() -> list[FormTag]:
    forms: list[FormTag] = []
    for path in sorted(_TEMPLATES.rglob("*.html")):
        masked, blocks = _mask_jinja(path.read_text(encoding="utf-8"))
        parser = _FormCollector()
        parser.feed(masked)
        for method, action in parser.forms:
            if action is None:
                continue  # an htmx-only form: no plain submit target
            forms.append(
                FormTag(
                    template=path.relative_to(_TEMPLATES).as_posix(),
                    method=method,
                    action=_unmask(action, blocks).strip(),
                )
            )
    return forms


def _concrete_paths(action: str) -> list[str]:
    """Turn a templated action into the concrete paths it can render as."""
    variants = [action]
    while any(_IF_ELSE.search(v) for v in variants):
        expanded: list[str] = []
        for v in variants:
            m = _IF_ELSE.search(v)
            if m is None:
                expanded.append(v)
                continue
            expanded.append(v[: m.start()] + m.group(1) + v[m.end() :])
            expanded.append(v[: m.start()] + m.group(2) + v[m.end() :])
        variants = expanded
    paths = []
    for v in variants:
        v = _SEGMENT_EXPR.sub("1", v)
        v = _ANY_STMT.sub("", _ANY_EXPR.sub("", v))
        paths.append(v.split("?", 1)[0])
    return paths


def _route_for(app_routes: list[object], method: str, path: str) -> bool:
    scope = {"type": "http", "method": method.upper(), "path": path, "root_path": ""}
    return any(route.matches(scope)[0] is Match.FULL for route in app_routes)  # type: ignore[attr-defined]


_FORMS = _collect_forms()


def test_the_scan_finds_the_forms() -> None:
    """Guard against a parser change that silently finds nothing."""
    assert len(_FORMS) > 40
    assert any(f.template == "_partials/camera_row.html" for f in _FORMS)
    assert any(f.template == "_partials/milestone_form.html" for f in _FORMS)


def test_concrete_paths() -> None:
    assert _concrete_paths("/cameras/{{ camera.id }}/delete") == ["/cameras/1/delete"]
    assert _concrete_paths(
        "/projects/{{ p.id }}/frames/{{ f.id }}/restore{{ ap_q }}"
    ) == ["/projects/1/frames/1/restore"]
    assert _concrete_paths("/x/{{ a }}?drawer=1") == ["/x/1"]
    assert _concrete_paths("{% if e %}/a/{{ i }}/edit{% else %}/a{% endif %}") == [
        "/a/1/edit",
        "/a",
    ]


def test_every_form_action_has_a_route(web_client: TestClient) -> None:
    routes = list(web_client.app.routes)  # type: ignore[attr-defined]
    problems: list[str] = []
    for form in _FORMS:
        if (form.template, form.action) in _DYNAMIC_ACTIONS:
            continue
        if _ANY_EXPR.fullmatch(form.action):
            problems.append(
                f"{form.template}: fully dynamic action {form.action!r}; "
                "add it to _DYNAMIC_ACTIONS if that is intended"
            )
            continue
        for path in _concrete_paths(form.action):
            if not _route_for(routes, form.method, path):
                problems.append(
                    f"{form.template}: <form method={form.method!r} "
                    f"action={form.action!r}> has no {form.method.upper()} "
                    f"route for {path!r}"
                )
    assert not problems, "\n".join(problems)

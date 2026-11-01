"""Static contracts on the frontend sources (#70).

Created by WP-70-C (F-S5); later #70 work packages add their own contracts here.

**HTML sinks.** Assigning markup to ``innerHTML``/``outerHTML``, or calling
``insertAdjacentHTML``/``document.write``, parses a string as HTML in the live
page. Script that builds UI from response data must use ``createElement``,
``textContent`` and ``dataset`` instead, so server or user text can never become
markup. Every remaining sink is listed in :data:`_SINK_ALLOWLIST` with the reason
it is safe, keyed by file **and** the exact line, so a new sink in an allowlisted
file still fails.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

_JS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "timelapse_manager"
    / "web"
    / "static"
    / "js"
)
# Vendored, minified third-party code is not ours to rewrite (see WP-70-N).
_VENDORED = frozenset({"htmx.min.js"})

# An assignment (or +=) to innerHTML/outerHTML, never a comparison, a read, or
# the string "innerHTML" used as an htmx swap style.
_ASSIGN_SINK = re.compile(r"\.(?:inner|outer)HTML\s*\+?=(?!=)")
_CALL_SINK = re.compile(r"\binsertAdjacentHTML\s*\(|\bdocument\.write(?:ln)?\s*\(")

# (file name, stripped source line) -> why it is safe.
_SINK_ALLOWLIST: dict[tuple[str, str], str] = {
    ("selection.js", "bar.outerHTML = selectionBarHTML;"): (
        "restores selectionBarHTML, a snapshot of the server-rendered action bar "
        "taken from the live DOM (captureSelectionBar); never response data"
    ),
    ("modal.js", "content.innerHTML = placeholders.get(content);"): (
        "restores the modal's own server-rendered placeholder markup, captured "
        "from the live DOM before the first swap; never response data"
    ),
}


def _js_sources() -> list[Path]:
    files = sorted(p for p in _JS_DIR.glob("*.js") if p.name not in _VENDORED)
    assert files, f"no JS sources found under {_JS_DIR}"
    return files


def _sink_lines() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in _js_sources():
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            code = line.strip()
            if code.startswith(("//", "*", "/*")):
                continue
            if _ASSIGN_SINK.search(code) or _CALL_SINK.search(code):
                hits.append((path.name, lineno, code))
    return hits


class TestHtmlSinks:
    def test_no_unlisted_html_sinks(self) -> None:
        unlisted = [
            f"{name}:{lineno}: {code}"
            for name, lineno, code in _sink_lines()
            if (name, code) not in _SINK_ALLOWLIST
        ]
        assert not unlisted, (
            "HTML sink outside the allowlist; build the DOM with createElement/"
            "textContent, or add the line to _SINK_ALLOWLIST with the reason it "
            "is safe:\n" + "\n".join(unlisted)
        )

    def test_allowlist_has_no_stale_entries(self) -> None:
        present = {(name, code) for name, _, code in _sink_lines()}
        stale = sorted(set(_SINK_ALLOWLIST) - present)
        assert not stale, f"allowlisted sinks no longer in the source: {stale}"

    @pytest.mark.parametrize(
        "snippet",
        [
            "el.innerHTML = html;",
            "el.innerHTML += more;",
            "bar.outerHTML=markup;",
            "el.insertAdjacentHTML('beforeend', x);",
            "document.write(x);",
        ],
    )
    def test_pattern_catches_sinks(self, snippet: str) -> None:
        assert _ASSIGN_SINK.search(snippet) or _CALL_SINK.search(snippet)

    @pytest.mark.parametrize(
        "snippet",
        [
            'swap: "innerHTML",',
            "if (el.innerHTML === '') {",
            "var copy = bar.outerHTML;",
            "placeholders.set(content, content.innerHTML);",
        ],
    )
    def test_pattern_ignores_non_sinks(self, snippet: str) -> None:
        assert not (_ASSIGN_SINK.search(snippet) or _CALL_SINK.search(snippet))


class TestMutationScriptsUseGuardedRequests:
    """The frames page's JSON mutations go through net.js (#70 F-R2)."""

    def test_net_js_loads_before_its_users(self) -> None:
        base = (_JS_DIR.parent.parent / "templates" / "base.html").read_text("utf-8")
        order = [
            base.index(f"static_url('js/{name}')")
            for name in ("htmx-errors.js", "net.js", "scrubber.js", "selection.js")
        ]
        assert order == sorted(order)

    @pytest.mark.parametrize("name", ["selection.js", "scrubber.js"])
    def test_no_raw_fetch(self, name: str) -> None:
        source = (_JS_DIR / name).read_text("utf-8")
        assert not re.search(r"(?<![\w.])fetch\s*\(", source), (
            f"{name} calls fetch() directly; use window.tlmRequest (net.js)"
        )


# ---------------------------------------------------------------------------
# Inline script and handler contract (#70 F-S3, #67-S17; WP-70-M1).
#
# A strict ``script-src`` refuses inline ``<script>`` blocks and ``on*=``
# attributes, so the templates must not carry them: behaviour lives in
# static/js and is reached through ``data-action`` (actions.js). The
# conversion is staged, so the files not converted yet are listed below with
# the most they may still contain; WP-70-M3 emptied the table. An entry can
# only shrink: a new inline script or
# handler anywhere fails the test, and so does an entry that no longer matches
# anything.
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = _JS_DIR.parent.parent / "templates"

# Jinja comments, then expressions and statements, neutralised so the markup
# around them parses: an expression becomes plain text, a statement vanishes.
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_JINJA_STMT = re.compile(r"\{%.*?%\}", re.DOTALL)

# Attributes that run script from markup: on*= handlers, htmx's eval-based
# hx-on, and javascript: URLs.
_HANDLER_ATTR = re.compile(r"^(?:on[a-z]+|(?:data-)?hx-on\b.*)$")
# A <script type> that is not executable (a data block) is not a CSP concern.
_DATA_SCRIPT_TYPES = frozenset({"application/json", "application/ld+json"})


class _InlineCodeFinder(HTMLParser):
    """Count inline ``<script>`` blocks and script-bearing attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts = 0
        self.handlers = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name.lower(): value for name, value in attrs}
        script_type = (names.get("type") or "").lower()
        if (
            tag == "script"
            and "src" not in names
            and script_type not in _DATA_SCRIPT_TYPES
        ):
            self.scripts += 1
        for name, value in names.items():
            if _HANDLER_ATTR.match(name) or (
                (value or "").strip().lower().startswith("javascript:")
            ):
                self.handlers += 1


def _inline_code(source: str) -> tuple[int, int]:
    """Return ``(inline scripts, script-bearing attributes)`` in a template."""
    source = _JINJA_COMMENT.sub("", source)
    source = _JINJA_EXPR.sub("x", source)
    source = _JINJA_STMT.sub("", source)
    finder = _InlineCodeFinder()
    finder.feed(source)
    finder.close()
    return finder.scripts, finder.handlers


def _templates_inline_code() -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for path in sorted(_TEMPLATE_DIR.rglob("*.html")):
        counts = _inline_code(path.read_text("utf-8"))
        if counts != (0, 0):
            found[path.relative_to(_TEMPLATE_DIR).as_posix()] = counts
    return found


# template path -> (inline scripts, script-bearing attributes) still allowed.
# Empty since WP-70-M3: the CSP header carries no 'script-src' 'unsafe-inline',
# so no template may hold inline script. Do not add entries.
_INLINE_ALLOWLIST: dict[str, tuple[int, int]] = {}


class TestNoInlineScriptOrHandlers:
    def test_converted_templates_are_clean(self) -> None:
        # The project and schedule partials (WP-70-M1) and the camera, user and
        # frame templates (WP-70-M2) stay clean.
        found = _templates_inline_code()
        for name in (
            "_partials/capture_schedule_fields.html",
            "_partials/_exact_time_fieldset.html",
            "_partials/_event_triggers_fieldset.html",
            "_partials/new_project_form.html",
            "_partials/project_settings_form.html",
            "_partials/milestone_form.html",
            # WP-70-M2: the camera, user, frame and misc partials.
            "_partials/camera_form.html",
            "_partials/camera_geo_poll_result.html",
            "_partials/camera_query_results.html",
            "_partials/frame_metadata.html",
            "_partials/frame_timestamp_row.html",
            "_partials/frames_import_form.html",
            "_partials/new_user_form.html",
            "_partials/toast.html",
            "_partials/user_form.html",
            "_partials/user_row.html",
            "frames.html",
            "users.html",
        ):
            assert name not in found, f"{name} has inline script or handlers"

    def test_no_new_inline_script_or_handlers(self) -> None:
        grown = {
            name: (counts, _INLINE_ALLOWLIST.get(name, (0, 0)))
            for name, counts in _templates_inline_code().items()
            if counts[0] > _INLINE_ALLOWLIST.get(name, (0, 0))[0]
            or counts[1] > _INLINE_ALLOWLIST.get(name, (0, 0))[1]
        }
        assert not grown, (
            "inline <script> or on*= handler outside the allowlist; register a "
            "data-action in static/js (actions.js) instead:\n"
            + "\n".join(
                f"{name}: found {counts}, allowed {allowed}"
                for name, (counts, allowed) in sorted(grown.items())
            )
        )

    def test_allowlist_has_no_stale_entries(self) -> None:
        found = _templates_inline_code()
        stale = sorted(name for name in _INLINE_ALLOWLIST if name not in found)
        assert not stale, f"allowlisted templates are now clean; remove them: {stale}"

    @pytest.mark.parametrize(
        ("markup", "expected"),
        [
            ("<script>1</script>", (1, 0)),
            ('<script src="/a.js"></script>', (0, 0)),
            ('<script type="application/json">{}</script>', (0, 0)),
            ('<button onclick="x()">', (0, 1)),
            ("<input onchange='x()' {% if a %}checked{% endif %}>", (0, 1)),
            ('<div hx-on:click="x()">', (0, 1)),
            ('<a href="javascript:void(0)">', (0, 1)),
            ('<a href="{{ url }}" data-action="x">', (0, 0)),
            ('{# <script>1</script> onclick="x" #}<p class="a">', (0, 0)),
            ('<p title="done">one</p>', (0, 0)),
        ],
    )
    def test_detector(self, markup: str, expected: tuple[int, int]) -> None:
        assert _inline_code(markup) == expected


class TestNoInlineStyleBlocks:
    """The import form's ``<style>`` moved to frames.css (WP-70-M2)."""

    def test_import_form_has_no_style_block(self) -> None:
        source = (_TEMPLATE_DIR / "_partials" / "frames_import_form.html").read_text(
            "utf-8"
        )
        assert "<style" not in source
        css = (_JS_DIR.parent / "css" / "screens" / "frames.css").read_text("utf-8")
        assert ".frames-import-disclosure > summary" in css


class _FormFieldOrder(HTMLParser):
    """Record the ``name`` of each form control in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("input", "select", "textarea"):
            names = dict(attrs)
            self.fields.append((names.get("name") or "", names.get("type") or tag))


class TestImportFormCsrfFirst:
    """The CSRF middleware reads only the first multipart part (WP-67-N), so a
    no-JS import must send ``csrf_token`` before the file input."""

    def test_csrf_token_precedes_every_other_field(self) -> None:
        source = (_TEMPLATE_DIR / "_partials" / "frames_import_form.html").read_text(
            "utf-8"
        )
        parser = _FormFieldOrder()
        parser.feed(_JINJA_COMMENT.sub("", _JINJA_STMT.sub("", source)))
        names = [name for name, _ in parser.fields]
        assert names[0] == "csrf_token", names
        assert parser.fields[0][1] == "hidden"
        assert names.count("csrf_token") == 1
        assert "files" in names and names.index("files") > 0


# ---------------------------------------------------------------------------
# Rendered pages carry no inline script (WP-70-M3).
# ---------------------------------------------------------------------------


class TestBootstrapScriptsAreExternal:
    def test_theme_init_is_loaded_in_head_before_the_stylesheet(self) -> None:
        base = (_TEMPLATE_DIR / "base.html").read_text("utf-8")
        script = base.index("js/theme-init.js")
        assert script < base.index("css/app.css")
        assert script < base.index("</head>")

    @pytest.mark.parametrize(
        "name",
        ["login.html", "first_run.html", "errors/403.html", "errors/generic.html"],
    )
    def test_standalone_pages_load_theme_init_before_the_stylesheet(
        self, name: str
    ) -> None:
        source = (_TEMPLATE_DIR / name).read_text("utf-8")
        assert source.index("js/theme-init.js") < source.index("css/app.css")

    def test_html_root_carries_the_per_request_values(self) -> None:
        base = (_TEMPLATE_DIR / "base.html").read_text("utf-8")
        root = base[base.index("<html") : base.index(">", base.index("<html"))]
        assert 'data-theme-pref="{{ user_theme }}"' in root
        assert "data-viewer-tz=" in root

    def test_htmx_config_refuses_script_tags(self) -> None:
        base = (_TEMPLATE_DIR / "base.html").read_text("utf-8")
        assert '"allowScriptTags":false' in base
        assert '"allowEval":false' in base


class TestRenderedPagesAreInlineFree:
    """The responses themselves, not only the templates, carry no inline code."""

    @staticmethod
    def _assert_strict(response) -> None:  # type: ignore[no-untyped-def]
        assert _inline_code(response.text) == (0, 0)
        assert "js/theme-init.js" in response.text
        csp = response.headers["Content-Security-Policy"]
        assert "script-src 'self';" in csp

    def test_login_and_first_run(
        self,
        anon_client,
        web_client,  # type: ignore[no-untyped-def]
    ) -> None:
        self._assert_strict(anon_client.get("/login"))

    @pytest.mark.parametrize("name", ["errors/403.html", "errors/generic.html"])
    def test_error_pages(self, admin_client, name: str) -> None:  # type: ignore[no-untyped-def]
        # The handlers render these from the exception alone, so render them
        # directly (``admin_client`` has installed the asset manifest).
        from timelapse_manager.web.dependencies import templates

        html = templates.get_template(name).render(detail="nope")
        assert _inline_code(html) == (0, 0)
        assert "js/theme-init.js" in html

    def test_signed_in_shell_carries_the_html_root_values(
        self,
        admin_client,  # type: ignore[no-untyped-def]
    ) -> None:
        response = admin_client.get("/")
        assert _inline_code(response.text) == (0, 0)
        assert "data-theme-pref=" in response.text
        assert "data-viewer-tz=" in response.text

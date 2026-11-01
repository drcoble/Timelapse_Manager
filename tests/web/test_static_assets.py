"""Versioned static URLs and the in-memory CSS bundle (#70 F-R8, F-P5).

* Every page in the base and standalone set references ``/static/`` only via
  ``?v=<content hash>`` URLs, and the font preload is byte-identical to the
  font URL inside the bundle.
* The bundle resolves the whole ``@import`` chain in cascade order, versions its
  font URLs, and is served from memory with an ``ETag``.
* ``Cache-Control`` is ``immutable`` only for the current ``v``; a stale or
  missing ``v`` revalidates (``no-cache``); an unversioned font gets a month.
* A manifest built from a ``tmp_path`` tree changes hash when content changes,
  and bad trees (cycles, escapes, missing files) fail at build time.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_admin
from timelapse_manager.web.assets import (
    BUNDLE_PATH,
    CACHE_FONT_UNVERSIONED,
    CACHE_IMMUTABLE,
    CACHE_REVALIDATE,
    AssetError,
    AssetManifest,
    UnknownAssetError,
    current_manifest,
    static_url,
)
from timelapse_manager.web.dependencies import templates

_STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "timelapse_manager" / "web" / "static"
)
_VERSIONED_RE = re.compile(r"^/static/[\w./-]+\?v=[0-9a-f]{12}$")
_STATIC_REF_RE = re.compile(r"""(?:href|src)="(/static/[^"]*)\"""")


def _assert_only_versioned_static(html: str) -> list[str]:
    """Every ``/static/`` reference in ``html`` is a ``?v=`` URL; return them."""
    refs = _STATIC_REF_RE.findall(html)
    assert refs, "page references no static assets"
    unversioned = [ref for ref in refs if not _VERSIONED_RE.match(ref)]
    assert unversioned == []
    # No /static/ reference outside href/src either (inline style, script).
    assert html.count("/static/") == len(refs)
    return refs


def _preload_href(html: str) -> str:
    match = re.search(r'<link rel="preload" as="font"[^>]*href="([^"]+)"', html)
    assert match is not None, "no font preload"
    assert "crossorigin" in match.group(0)
    return match.group(1)


def _bundle(client: TestClient) -> str:
    resp = client.get(static_url("css/app.css"))
    assert resp.status_code == 200
    return resp.text


# ---------------------------------------------------------------------------
# Rendered pages
# ---------------------------------------------------------------------------


class TestPagesUseVersionedUrls:
    @pytest.mark.parametrize("path", ["/", "/projects", "/about", "/renders"])
    def test_base_pages(self, admin_client: TestClient, path: str) -> None:
        html = admin_client.get(path).text
        refs = _assert_only_versioned_static(html)
        assert static_url("css/app.css") in refs
        assert static_url("js/htmx.min.js") in refs
        assert static_url("js/selection.js") in refs

    def test_login_page(self, anon_client: TestClient) -> None:
        html = anon_client.get("/login").text
        _assert_only_versioned_static(html)
        assert _preload_href(html) == static_url("fonts/Geist-Variable.woff2")

    def test_first_run_page(self, web_client: TestClient) -> None:
        html = web_client.get("/first-run").text
        _assert_only_versioned_static(html)
        assert _preload_href(html) == static_url("fonts/Geist-Variable.woff2")

    @pytest.mark.parametrize("name", ["errors/403.html", "errors/generic.html"])
    def test_error_pages(self, admin_client: TestClient, name: str) -> None:
        # The error pages render from the exception alone (no request state),
        # so render them directly; ``admin_client`` has installed the manifest.
        html = templates.get_template(name).render(detail="nope")
        _assert_only_versioned_static(html)

    def test_preload_matches_the_font_url_in_the_bundle(
        self, admin_client: TestClient
    ) -> None:
        href = _preload_href(admin_client.get("/").text)
        assert f'url("{href}")' in _bundle(admin_client)

    def test_unknown_asset_fails_the_render(self, admin_client: TestClient) -> None:
        template = templates.env.from_string("{{ static_url('js/no-such.js') }}")
        with pytest.raises(UnknownAssetError):
            template.render()


# ---------------------------------------------------------------------------
# The served bundle
# ---------------------------------------------------------------------------


def _import_order(app_css: str) -> list[str]:
    """The ``@import`` targets of ``app.css`` in source order (comments skipped)."""
    no_comments = re.sub(r"/\*.*?\*/", "", app_css, flags=re.S)
    return [
        "css/" + target
        for target in re.findall(r"@import\s+url\('([^']+)'\)\s*;", no_comments)
    ]


class TestBundle:
    def test_bundle_has_no_imports_and_versioned_fonts(
        self, anon_client: TestClient
    ) -> None:
        css = _bundle(anon_client)
        assert "@import" not in css
        font_urls = re.findall(r'url\("(/static/fonts/[^"]+)"\)', css)
        assert sorted(font_urls) == sorted(
            [
                static_url("fonts/Geist-Variable.woff2"),
                static_url("fonts/GeistMono-Variable.woff2"),
            ]
        )
        # The inline SVG data: URIs in tokens.css pass through untouched.
        assert css.count('url("data:image/svg+xml,') == 4

    def test_bundle_keeps_the_import_order(self, anon_client: TestClient) -> None:
        css = _bundle(anon_client)
        expected = _import_order((_STATIC_DIR / "css" / "app.css").read_text())
        assert len(expected) == 37
        markers = re.findall(r"^/\* (css/[\w/.-]+) \*/$", css, flags=re.M)
        assert markers == expected
        # responsive.css must stay last: its @media overrides win by order.
        assert markers[-1] == "css/responsive.css"

    def test_raw_app_css_is_still_served_unchanged(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get("/static/css/app.css")
        assert resp.status_code == 200
        assert resp.text == (_STATIC_DIR / "css" / "app.css").read_text()

    def test_bundle_served_from_memory_with_etag_and_immutable(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get(static_url("css/app.css"))
        assert resp.headers["content-type"].startswith("text/css")
        assert resp.headers["cache-control"] == CACHE_IMMUTABLE
        etag = resp.headers["etag"]
        assert etag == f'"{current_manifest().bundle_hash}"'
        assert resp.content == current_manifest().bundle

        again = anon_client.get(
            static_url("css/app.css"), headers={"If-None-Match": etag}
        )
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["cache-control"] == CACHE_IMMUTABLE

    def test_bundle_head_has_length_but_no_body(self, anon_client: TestClient) -> None:
        resp = anon_client.head(static_url("css/app.css"))
        assert resp.status_code == 200
        assert resp.content == b""
        assert int(resp.headers["content-length"]) == len(current_manifest().bundle)

    def test_bundle_rejects_other_methods(self, anon_client: TestClient) -> None:
        resp = anon_client.post(f"/static/{BUNDLE_PATH}")
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Cache-Control on /static/
# ---------------------------------------------------------------------------


class TestStaticCacheControl:
    def test_current_version_is_immutable(self, anon_client: TestClient) -> None:
        resp = anon_client.get(static_url("js/selection.js"))
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == CACHE_IMMUTABLE

    def test_stale_version_revalidates_and_serves_current_content(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get("/static/js/selection.js?v=stale0000000")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == CACHE_REVALIDATE
        assert resp.content == (_STATIC_DIR / "js" / "selection.js").read_bytes()

    def test_missing_version_revalidates(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/static/js/selection.js")
        assert resp.headers["cache-control"] == CACHE_REVALIDATE

    def test_stale_bundle_version_revalidates(self, anon_client: TestClient) -> None:
        resp = anon_client.get(f"/static/{BUNDLE_PATH}?v=stale0000000")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == CACHE_REVALIDATE

    def test_unversioned_font_is_cached_for_a_month(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get("/static/fonts/Geist-Variable.woff2")
        assert resp.headers["cache-control"] == CACHE_FONT_UNVERSIONED
        stale = anon_client.get("/static/fonts/Geist-Variable.woff2?v=stale0000000")
        assert stale.headers["cache-control"] == CACHE_REVALIDATE

    def test_conditional_get_on_a_file_keeps_the_policy(
        self, anon_client: TestClient
    ) -> None:
        url = static_url("js/selection.js")
        etag = anon_client.get(url).headers["etag"]
        resp = anon_client.get(url, headers={"If-None-Match": etag})
        assert resp.status_code == 304
        assert resp.headers["cache-control"] == CACHE_IMMUTABLE

    def test_unknown_file_is_404(self, web_client: TestClient) -> None:
        seed_admin(web_client)
        assert web_client.get("/static/js/no-such.js?v=0").status_code == 404


# ---------------------------------------------------------------------------
# Manifest built from a directory
# ---------------------------------------------------------------------------


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


_MINIMAL = {
    "css/app.css": (
        "/* index: the @import below is prose, @import url('nope.css'); */\n"
        "@import url('a.css');\n"
        '@import "sub/b.css" screen and (max-width: 600px);\n'
    ),
    "css/a.css": '.a { background: url("../img/x.png"); content: "/* kept */"; }\n',
    "css/sub/b.css": ".b { src: url(/static/img/x.png); mask: url(#m); }\n",
    "img/x.png": "png-bytes",
    "js/app.js": "console.log(1);\n",
}


class TestManifestFromDirectory:
    def test_hash_changes_when_content_changes(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, _MINIMAL)
        first = AssetManifest.build(root)
        (root / "js" / "app.js").write_text("console.log(2);\n")
        second = AssetManifest.build(root)
        assert first.url("js/app.js") != second.url("js/app.js")
        assert first.bundle_hash == second.bundle_hash

        (root / "css" / "a.css").write_text(".a { color: red; }\n")
        third = AssetManifest.build(root)
        assert third.bundle_hash != second.bundle_hash
        assert third.url("css/app.css") != second.url("css/app.css")

    def test_bundle_resolves_imports_media_and_urls(self, tmp_path: Path) -> None:
        manifest = AssetManifest.build(_tree(tmp_path, _MINIMAL))
        css = manifest.bundle.decode()
        img = manifest.url("img/x.png")
        assert "@import" not in css
        assert css.index("/* css/a.css */") < css.index("/* css/sub/b.css */")
        assert "@media screen and (max-width: 600px) {" in css
        assert css.count(f'url("{img}")') == 2
        assert "url(#m)" in css
        assert '"/* kept */"' in css
        assert "nope.css" not in css

    def test_url_maps_app_css_to_the_bundle(self, tmp_path: Path) -> None:
        manifest = AssetManifest.build(_tree(tmp_path, _MINIMAL))
        assert manifest.url("css/app.css") == (
            f"/static/{BUNDLE_PATH}?v={manifest.bundle_hash}"
        )

    def test_unknown_path_raises(self, tmp_path: Path) -> None:
        manifest = AssetManifest.build(_tree(tmp_path, _MINIMAL))
        with pytest.raises(UnknownAssetError):
            manifest.url("js/missing.js")

    @pytest.mark.parametrize(
        ("files", "message"),
        [
            (
                {
                    "css/app.css": "@import url('a.css');",
                    "css/a.css": "@import url('app.css');",
                },
                "cycle",
            ),
            ({"css/app.css": "@import url('missing.css');"}, "does not exist"),
            ({"css/app.css": "@import url('../../etc/passwd');"}, "escapes"),
            ({"css/app.css": ".x { background: url('/etc/passwd'); }"}, "outside"),
            ({"css/app.css": ".x { background: url('gone.png'); }"}, "does not exist"),
            ({"css/app.css": "@import url('https://example.com/x.css');"}, "non-local"),
            (
                {"css/app.css": "@import url('a.css') layer(base);", "css/a.css": ""},
                "not supported",
            ),
            ({"css/other.css": ""}, "missing"),
            ({"css/app.css": "", "css/app.bundle.css": ""}, "reserved"),
        ],
    )
    def test_bad_trees_fail_at_build(
        self, tmp_path: Path, files: dict[str, str], message: str
    ) -> None:
        with pytest.raises(AssetError, match=message):
            AssetManifest.build(_tree(tmp_path, files))

    def test_real_static_tree_builds(self) -> None:
        manifest = AssetManifest.build(_STATIC_DIR)
        # Deterministic: a rebuild of unchanged files yields the same URLs.
        assert AssetManifest.build(_STATIC_DIR).hashes == manifest.hashes
        assert manifest.url("js/htmx.min.js").startswith("/static/js/htmx.min.js?v=")


def test_static_url_is_a_template_global() -> None:
    assert isinstance(templates.env, jinja2.Environment)
    assert templates.env.globals["static_url"] is static_url


def test_manifest_falls_back_to_the_package_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app that never ran ``mount_web`` still renders versioned URLs."""
    from timelapse_manager.web import assets

    monkeypatch.setattr(assets, "_manifest", None)
    html = templates.get_template("errors/generic.html").render(detail="x")
    _assert_only_versioned_static(html)
    assert current_manifest().hashes == AssetManifest.build(_STATIC_DIR).hashes

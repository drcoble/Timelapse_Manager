"""Design-token guardrails: presence of load-bearing tokens and WCAG contrast.

These assertions track the served token stylesheet directly so a future edit
that drops a carried-forward token or regresses a contrast pair fails fast.
"""

from __future__ import annotations

from pathlib import Path

import timelapse_manager.web as web_pkg

TOKENS_CSS = Path(web_pkg.__file__).parent / "static" / "css" / "tokens.css"

# Tokens that predate the palette rework and are still referenced by component
# and screen styles — they must survive any token-file rewrite.
CARRIED_FORWARD = [
    "--color-success",
    "--color-warning",
    "--color-danger",
    "--color-info",
    "--color-info-bg",
    "--color-warning-desc",
    "--color-error-desc",
    "--color-info-desc",
    "--color-success-desc",
    "--color-select-arrow-url",
    "--color-admin-border",
    "--color-auth-ldap-border",
    "--nav-width",
    "--header-height",
    "--radius-sm",
    "--radius-md",
    "--radius-lg",
    "--sp-1",
    "--sp-12",
    "--transition-fast",
    "--transition-mid",
    "--font-sans",
    "--font-mono",
]

# Tokens introduced by the palette rework.
NEW_TOKENS = [
    "--text-2xs",
    "--text-xs",
    "--text-sm",
    "--text-md",
    "--text-lg",
    "--text-xl",
    "--text-2xl",
    "--radius-xl",
    "--sp-14",
    "--sp-16",
    "--color-ribbon-day",
    "--color-ribbon-night",
    "--color-ribbon-golden",
    "--color-ribbon-gap",
    "--color-ribbon-render",
    "--color-device-bg",
    "--color-device-text",
    "--color-device-border",
    "--color-accent-text",
]


def _css() -> str:
    return TOKENS_CSS.read_text(encoding="utf-8")


def test_carried_forward_tokens_present() -> None:
    css = _css()
    missing = [t for t in CARRIED_FORWARD if f"{t}:" not in css]
    assert not missing, f"carried-forward tokens dropped: {missing}"


def test_new_tokens_present() -> None:
    css = _css()
    missing = [t for t in NEW_TOKENS if f"{t}:" not in css]
    assert not missing, f"new tokens missing: {missing}"


def test_fonts_use_geist() -> None:
    css = _css()
    assert '"Geist"' in css
    assert '"Geist Mono"' in css


# --- WCAG 2.1 relative-luminance contrast ---------------------------------


def _channel(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast(fg: str, bg: str) -> float:
    lf, lb = _luminance(fg), _luminance(bg)
    hi, lo = max(lf, lb), min(lf, lb)
    return (hi + 0.05) / (lo + 0.05)


# (foreground, background, label) — worst-case pairings for normal-size text.
AA_PAIRS = [
    ("#c8a96e", "#0c0d14", "dark accent on bg"),
    ("#84899f", "#1c1f2e", "dark text-dim on surface-2"),
    ("#161310", "#a07830", "light on-accent on accent (button label)"),
    ("#7a5a20", "#f5f4f0", "light accent-text on bg"),
    ("#6a6258", "#ffffff", "light text-dim on surface"),
]


def test_corrected_pairs_meet_wcag_aa_normal() -> None:
    failures = []
    for fg, bg, label in AA_PAIRS:
        ratio = _contrast(fg, bg)
        if ratio < 4.5:
            failures.append(f"{label}: {fg} on {bg} = {ratio:.2f}:1")
    assert not failures, "contrast below AA 4.5:1 -> " + "; ".join(failures)


def test_contrast_values_present_in_tokens() -> None:
    """The audited values must actually be the ones shipped in tokens.css."""
    css = _css()
    for value in ("#84899f", "#161310", "#7a5a20", "#6a6258", "#c8a96e", "#0c0d14"):
        assert value in css, f"audited value {value} not found in tokens.css"

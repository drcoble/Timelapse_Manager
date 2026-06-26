"""Overlay sanitisation: escaping user text and confining the watermark path.

Overlay content reaches ffmpeg as part of the ``-vf`` filtergraph string, which
has its own escaping rules layered on top of the shell-free argv. User-supplied
text (a caption, or an strftime timestamp pattern) must be escaped so a stray
``:``, ``\\``, ``'`` or ``%`` cannot break out of the ``drawtext`` option it sits
in. The watermark image path must be confined to the project's render root, the
same ``resolve()`` + ``is_relative_to`` check the output path uses, so an overlay
cannot read an arbitrary file.

Nothing here spawns a process or builds the full filtergraph; it produces the
escaped fragments and resolved coordinates the implementation assembles.
"""

from __future__ import annotations

from pathlib import Path

from .encoder import EncoderError, OverlayConfig

# Pixel margin from the chosen corner for text/timestamp overlays.
_MARGIN = 10

# Placement -> (x, y) expression for drawtext, in terms of ffmpeg's text-box
# metrics (``tw``/``th``) and frame metrics (``w``/``h``). Kept as expressions so
# they adapt to the rendered text size and output geometry.
_TEXT_POSITIONS: dict[str, tuple[str, str]] = {
    "top_left": (f"{_MARGIN}", f"{_MARGIN}"),
    "top_right": (f"w-tw-{_MARGIN}", f"{_MARGIN}"),
    "bottom_left": (f"{_MARGIN}", f"h-th-{_MARGIN}"),
    "bottom_right": (f"w-tw-{_MARGIN}", f"h-th-{_MARGIN}"),
}

# Placement -> (x, y) expression for the overlay (image) filter, where ``W``/``H``
# are the main frame and ``w``/``h`` the overlaid image.
_IMAGE_POSITIONS: dict[str, tuple[str, str]] = {
    "top_left": (f"{_MARGIN}", f"{_MARGIN}"),
    "top_right": (f"W-w-{_MARGIN}", f"{_MARGIN}"),
    "bottom_left": (f"{_MARGIN}", f"H-h-{_MARGIN}"),
    "bottom_right": (f"W-w-{_MARGIN}", f"H-h-{_MARGIN}"),
}

_DEFAULT_PLACEMENT = "top_left"


def escape_drawtext(text: str) -> str:
    """Escape a literal caption for a single-quote-wrapped ``drawtext`` value.

    The value is wrapped in single quotes by the caller. ffmpeg's filtergraph
    parser cannot represent a literal ``'`` inside a single-quoted string at all
    (no escape sequence reopens the quote cleanly), so a straight apostrophe is
    replaced with the typographic right single quotation mark ``’`` -- it renders
    as an apostrophe and never breaks the quoting. Inside the quotes, ``\\`` must
    still be doubled, ``:`` escaped (it otherwise ends the ``drawtext`` option),
    and ``%`` written as ``\\\\%`` so it survives to ``drawtext`` as a literal
    rather than starting a ``%{...}`` expansion. Newlines collapse to a space.

    Backslash is escaped first so the backslashes inserted afterwards are not
    themselves doubled.
    """
    cleaned = text.replace("\r", "").replace("\n", " ")
    cleaned = cleaned.replace("'", "’")
    cleaned = cleaned.replace("\\", "\\\\")
    cleaned = cleaned.replace(":", "\\:")
    cleaned = cleaned.replace("%", "\\\\%")
    return cleaned


def escape_timestamp_format(fmt: str) -> str:
    """Escape an strftime pattern for the ``%{pts:gmtime:0:<fmt>}`` expansion.

    The format sits as the fourth, innermost argument of a ``drawtext`` ``pts``
    expansion that itself lives inside a single-quoted ``text=`` value. Two
    parsers see this string in turn -- the filtergraph option parser and then the
    ``%{...}`` argument splitter -- so a literal ``:`` inside the user's format
    (e.g. between ``%H`` and ``%M``) must survive *both*: it is written as
    ``\\\\\\:`` (three backslashes) so one backslash reaches the ``%{...}``
    splitter, which then treats the colon as literal rather than an argument
    separator. The ``%`` directives must pass through to strftime untouched, so
    ``%`` is *not* escaped here (unlike :func:`escape_drawtext`). A single quote
    would close the surrounding ``text='...'`` quoting, so it is escaped; newlines
    are dropped.
    """
    cleaned = fmt.replace("\r", "").replace("\n", " ")
    cleaned = cleaned.replace("\\", "\\\\")
    cleaned = cleaned.replace(":", "\\\\\\:")
    cleaned = cleaned.replace("'", "\\'")
    return cleaned


def escape_path_for_filter(path: str) -> str:
    """Escape a filesystem path for a single-quoted filtergraph option value.

    Used for ``drawtext``'s ``fontfile='...'``. The path is wrapped in single
    quotes by the caller, so inside the quotes ``:`` is already literal (a Windows
    drive letter ``C:`` must *not* be backslash-escaped, or the path would be
    corrupted) and only ``\\`` needs doubling. A literal ``'`` cannot appear
    inside a single-quoted filtergraph string, so it is dropped defensively -- a
    font/image path containing an apostrophe is not supported.
    """
    cleaned = path.replace("\\", "\\\\")
    cleaned = cleaned.replace("'", "")
    return cleaned


def text_position(placement: str) -> tuple[str, str]:
    """Return the ``drawtext`` ``(x, y)`` expressions for a placement corner."""
    return _TEXT_POSITIONS.get(placement, _TEXT_POSITIONS[_DEFAULT_PLACEMENT])


def image_position(placement: str) -> tuple[str, str]:
    """Return the ``overlay`` ``(x, y)`` expressions for a placement corner."""
    return _IMAGE_POSITIONS.get(placement, _IMAGE_POSITIONS[_DEFAULT_PLACEMENT])


def resolve_overlay_image(image_path: str, project_render_root: Path) -> Path:
    """Resolve and confine the watermark image path to the render root.

    The path is resolved to an absolute, symlink-free location and required to
    lie within ``project_render_root`` (resolved the same way). The check uses
    :meth:`pathlib.Path.is_relative_to`, never a string prefix comparison, so a
    ``..`` traversal or an absolute path outside the root is rejected. The file
    must also exist.

    :raises EncoderError: if the path escapes the render root or does not exist.
    """
    root = project_render_root.resolve()
    resolved = Path(image_path).resolve()
    if not resolved.is_relative_to(root):
        raise EncoderError(
            f"overlay image path is outside the project render root: {image_path!r}"
        )
    if not resolved.is_file():
        raise EncoderError(f"overlay image file does not exist: {image_path!r}")
    return resolved


def has_any_overlay(overlay: OverlayConfig) -> bool:
    """Return whether any overlay layer is enabled."""
    return overlay.timestamp_enabled or overlay.text_enabled or overlay.image_enabled

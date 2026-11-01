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

import re
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

# The timestamp overlay's format grammar (#67 S4); see validate_timestamp_format.
DEFAULT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
TIMESTAMP_FORMAT_MAX_LENGTH = 64
# The most characters a text-overlay caption may hold (#139). The caption is one
# argv element of the ffmpeg spawn, and Linux caps a single argument at 128 KiB.
TEXT_CONTENT_MAX_LENGTH = 1000
# The strftime directives a format may use, plus ``%%`` for a literal percent.
TIMESTAMP_DIRECTIVES = "aAbBdHIjmMpSyYzZ%"
# Explicit ASCII ranges: ``\w`` or ``str.isalnum`` would admit non-ASCII letters,
# and ``fullmatch`` (unlike ``^...$``) does not accept a trailing newline.
_TIMESTAMP_FORMAT_CHARS = re.compile(r"[A-Za-z0-9 _./:%-]+")


def _strip_controls(text: str) -> str:
    """Drop ASCII control characters, turning line breaks and tabs into spaces.

    A NUL cannot be carried in an argv element at all (the spawn would fail), and
    the other controls have no visible rendering, so none of them is worth
    passing to ffmpeg.
    """
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    text = text.replace("\t", " ")
    return "".join(ch for ch in text if ch >= " " and ch != "\x7f")


def escape_drawtext(text: str) -> str:
    """Escape a literal caption for a single-quote-wrapped ``drawtext`` value.

    The value passes through three ffmpeg parsers in turn, each removing one
    level of quoting or escaping (see ``av_get_token`` in libavutil):

    1. the filtergraph parser, which strips the surrounding ``'...'``; inside
       quotes everything except ``'`` itself is literal;
    2. the ``drawtext`` option parser, where an unescaped ``:`` would end the
       ``text`` option and start another (``\\:`` is a literal colon);
    3. drawtext's own text expansion, where ``\\`` escapes the next character
       and ``%`` starts a ``%{...}`` function.

    So:

    * ``'`` cannot be represented inside a single-quoted filtergraph string at
      all; it is replaced with the typographic ``’``, which renders the same.
    * ``\\`` is written as four backslashes: two for level 2 and two for level
      3. With only two, ``\\%{...}`` in a caption reached drawtext as a live
      expansion.
    * ``:`` is written as ``\\:`` (consumed at level 2).
    * ``%`` is written as ``\\\\%`` so drawtext receives ``\\%``, a literal
      percent, never a function call.
    * Line breaks and tabs become spaces and other control characters are
      dropped (:func:`_strip_controls`).

    Backslash is escaped first so the backslashes inserted afterwards are not
    themselves doubled.
    """
    cleaned = _strip_controls(text)
    cleaned = cleaned.replace("'", "\u2019")
    cleaned = cleaned.replace("\\", "\\\\\\\\")
    cleaned = cleaned.replace(":", "\\:")
    cleaned = cleaned.replace("%", "\\\\%")
    return cleaned


def validate_text_content(text: object) -> str:
    """Check a text-overlay caption's type and length; return it unchanged (#139).

    :raises ValueError: when it is not a string or is longer than
        :data:`TEXT_CONTENT_MAX_LENGTH` characters.
    """
    if not isinstance(text, str):
        raise ValueError("text_content must be a string")
    if len(text) > TEXT_CONTENT_MAX_LENGTH:
        raise ValueError(
            f"text_content must be at most {TEXT_CONTENT_MAX_LENGTH} characters"
        )
    return text


def validate_timestamp_format(fmt: str) -> str:
    """Check an strftime pattern for the timestamp overlay; return it unchanged.

    The format is embedded in a ``drawtext`` filter (see
    :func:`escape_timestamp_format`), so it is held to a small grammar instead of
    being escaped for every parser it passes through (#67 S4):

    * at most :data:`TIMESTAMP_FORMAT_MAX_LENGTH` characters, and not empty;
    * only ASCII letters and digits, space and ``_ . / : - %``;
    * every ``%`` starts one of the directives in
      :data:`TIMESTAMP_DIRECTIVES` (``%%`` is a literal percent).

    That rules out quotes, backslashes, ``,``, ``;``, ``[``, ``]``, ``{`` and
    control characters, so the value can neither leave its quoting nor start a
    drawtext ``%{...}`` function.

    :raises ValueError: with a short reason when the format is not allowed.
    """
    if not isinstance(fmt, str):
        raise ValueError("timestamp_format must be a string")
    if not fmt:
        raise ValueError("timestamp_format must not be empty")
    if len(fmt) > TIMESTAMP_FORMAT_MAX_LENGTH:
        raise ValueError(
            f"timestamp_format must be at most {TIMESTAMP_FORMAT_MAX_LENGTH} characters"
        )
    if _TIMESTAMP_FORMAT_CHARS.fullmatch(fmt) is None:
        raise ValueError(
            "timestamp_format may contain only letters, digits, spaces, "
            "strftime directives and _ . / : -"
        )
    index = 0
    while index < len(fmt):
        if fmt[index] == "%":
            directive = fmt[index + 1 : index + 2]
            if not directive or directive not in TIMESTAMP_DIRECTIVES:
                raise ValueError(
                    "timestamp_format: each % must start one of the directives "
                    f"%{' %'.join(TIMESTAMP_DIRECTIVES)}"
                )
            index += 2
        else:
            index += 1
    return fmt


def escape_timestamp_format(fmt: str) -> str:
    """Escape an strftime pattern for the ``%{pts:gmtime:0:<fmt>}`` expansion.

    The format is validated first (:func:`validate_timestamp_format`); every
    write path validates it too, so a failure here means an unchecked value
    reached the encoder, and the render fails rather than building a graph from
    it.

    After validation the only character special to any of the three parsers the
    format passes through (see :func:`escape_drawtext`) is ``:``. It is written
    as ``\\\\\\:`` (three backslashes): the filtergraph parser keeps all
    three (they are inside ``text='...'``), the option parser turns ``\\\\``
    into ``\\`` and ``\\:`` into ``:``, and the ``%{...}`` argument splitter
    then reads ``\\:`` as a literal colon instead of an argument separator. The
    ``%`` directives pass through to strftime untouched.

    :raises EncoderError: if the format does not pass validation.
    """
    try:
        validate_timestamp_format(fmt)
    except ValueError as exc:
        raise EncoderError(f"invalid timestamp overlay format: {exc}") from exc
    return fmt.replace(":", "\\\\\\:")


def escape_path_for_filter(path: str) -> str:
    """Escape a filesystem path for a single-quoted filtergraph option value.

    Used for ``drawtext``'s ``fontfile='...'``. The filtergraph parser removes
    the quotes and keeps everything inside literally; the option parser then
    reads the value unquoted, so ``\\`` is doubled and ``:`` written as
    ``\\:`` for it (without that, a Windows drive letter ``C:`` or any colon
    in the path would end the option). A literal ``'`` cannot appear inside a
    single-quoted filtergraph string, so it is dropped -- a font path containing
    an apostrophe is not supported. Control characters are dropped too.
    """
    cleaned = "".join(ch for ch in path if ch >= " " and ch != "\x7f")
    cleaned = cleaned.replace("\\", "\\\\")
    cleaned = cleaned.replace(":", "\\:")
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

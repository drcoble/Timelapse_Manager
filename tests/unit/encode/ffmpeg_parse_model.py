"""A test-only model of the ffmpeg text parsers our escaping targets (#67 S4/S15).

This Mac has no ffmpeg, so the escaping tests check what ffmpeg *would* read
back from the strings the encoder writes, using a small reference model of the
parsers involved. Each function is a direct transcription of the C code it
names (FFmpeg release/7.1), written independently of the production port in
``encode/allowlist.py`` so a bug in one is not silently mirrored by the other:

* ``av_get_token`` (libavutil/avstring.c): one level of quoting and escaping;
* ``avfilter_graph_segment_parse`` (libavfilter/graphparser.c): filter split;
* ``ff_filter_opt_parse`` / ``av_opt_get_key_value`` (libavfilter/avfilter.c,
  libavutil/opt.c): a filter's ``key=value:...`` options;
* ``ff_expand_text`` (libavfilter/textutils.c): drawtext's ``\\`` escapes and
  ``%{...}`` functions;
* ``read_line_to_bprint_escaped`` + ``unescape`` (libavformat/ffmetadec.c);
* the concat demuxer's line reader, ``get_keyword`` and the ``file`` argument
  token (libavformat/concatdec.c, libavformat/aviobuf.c).

A model is self-consistent, not proof: a real ``ffmpeg -filter_complex <graph>
-f null -`` parse in CI is what confirms it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WHITESPACES = " \n\t\r"
CONCAT_SPACE_CHARS = " \t\r\n"


def av_get_token(buf: str, term: str) -> tuple[str, str]:
    """Return ``(token, rest)``; ``rest`` starts at the terminator, if any."""
    i = 0
    while i < len(buf) and buf[i] in WHITESPACES:
        i += 1
    out = ""
    end = 0
    while i < len(buf) and buf[i] not in term:
        c = buf[i]
        i += 1
        if c == "\\" and i < len(buf):
            out += buf[i]
            i += 1
            end = len(out)
        elif c == "'":
            while i < len(buf) and buf[i] != "'":
                out += buf[i]
                i += 1
            if i < len(buf):
                i += 1
                end = len(out)
        else:
            out += c
    while len(out) > end and out[-1] in WHITESPACES:
        out = out[:-1]
    return out, buf[i:]


@dataclass
class ModelFilter:
    name: str
    args: str | None
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)


def _labels(buf: str) -> tuple[list[str], str]:
    labels: list[str] = []
    while buf.startswith("["):
        label, buf = av_get_token(buf[1:], "]")
        if not label or not buf.startswith("]"):
            raise ValueError("bad label")
        labels.append(label)
        buf = buf[1:].lstrip(WHITESPACES)
    return labels, buf


def parse_graph(graph: str) -> list[ModelFilter]:
    """Model of ``avfilter_graph_segment_parse``; raises ``ValueError``."""
    filters: list[ModelFilter] = []
    buf = graph.lstrip(WHITESPACES)
    while buf:
        inputs, buf = _labels(buf)
        name, buf = av_get_token(buf, "=,;[")
        args = None
        if buf.startswith("="):
            args, buf = av_get_token(buf[1:], "[],;")
        outputs, buf = _labels(buf)
        buf = buf.lstrip(WHITESPACES)
        filters.append(ModelFilter(name, args, inputs, outputs))
        if buf and buf[0] not in ",;":
            raise ValueError(f"trailing garbage: {buf!r}")
        buf = buf[1:].lstrip(WHITESPACES)
    return filters


def parse_options(args: str) -> list[tuple[str | None, str]]:
    """Model of ``ff_filter_opt_parse``'s key/value split (``=`` and ``:``)."""

    def is_key_char(c: str) -> bool:
        return c.isascii() and (c.isalnum() or c in "-_/.")

    pairs: list[tuple[str | None, str]] = []
    while args:
        stripped = args.lstrip(WHITESPACES)
        j = 0
        while j < len(stripped) and is_key_char(stripped[j]):
            j += 1
        after = stripped[j:].lstrip(WHITESPACES)
        key: str | None = None
        rest = args
        if after.startswith("="):
            key = stripped[:j]
            rest = after[1:]
        value, args = av_get_token(rest, ":")
        pairs.append((key, value))
        if args:
            args = args[1:]
    return pairs


def expand_drawtext(text: str) -> tuple[str, list[list[str]]]:
    """Model of ``ff_expand_text``: ``(literal text, [function argv, ...])``.

    Each function call is replaced in the literal text by ``\\x00`` so a test
    can tell where expansions happened.
    """
    out = ""
    calls: list[list[str]] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            out += text[i + 1]
            i += 2
        elif text[i] == "%":
            rest = text[i + 1 :]
            if not rest.startswith("{"):
                raise ValueError("stray %")
            rest = rest[1:]
            argv: list[str] = []
            while True:
                arg, rest = av_get_token(rest, ":}")
                argv.append(arg)
                if not rest:
                    raise ValueError("unterminated %{}")
                if rest[0] == "}":
                    break
                rest = rest[1:]
            calls.append(argv)
            out += "\x00"
            i = len(text) - len(rest) + 1
        else:
            out += text[i]
            i += 1
    return out, calls


def drawtext_options(graph: str) -> list[dict[str, str]]:
    """Return each drawtext's options, decoded through both parser levels."""
    result = []
    for f in parse_graph(graph):
        if f.name == "drawtext":
            result.append({k or "": v for k, v in parse_options(f.args or "")})
    return result


def read_ffmetadata(data: bytes) -> list[dict[str, str]]:
    """Model of the ffmetadata reader: one dict per ``[CHAPTER]`` section.

    The global section is the first dict.
    """
    lines: list[bytes] = []
    i = 0
    while i < len(data):
        line = b""
        prev = ord(" ")
        while i < len(data):
            c = data[i]
            i += 1
            end = prev != ord("\\") and c in (ord("\r"), ord("\n"), 0)
            prev = c
            if end:
                if c == ord("\r") and i < len(data) and data[i] == ord("\n"):
                    i += 1
                break
            line += bytes([c])
        lines.append(line)

    def unescape(raw: bytes) -> str:
        out = b""
        j = 0
        while j < len(raw):
            if raw[j] == ord("\\"):
                j += 1
            if j < len(raw):
                out += raw[j : j + 1]
            j += 1
        return out.decode("utf-8")

    sections: list[dict[str, str]] = [{}]
    for line in lines:
        if not line or line[:1] in (b";", b"#"):
            continue
        if line == b"[CHAPTER]":
            sections.append({})
            continue
        # read_tag: the key ends at the first unescaped '='.
        k = 0
        while k < len(line) and line[k] != ord("="):
            if line[k] == ord("\\"):
                k += 1
            k += 1
        if k >= len(line):
            continue
        sections[-1][unescape(line[:k])] = unescape(line[k + 1 :])
    return sections


def read_concat_files(text: str) -> list[str]:
    """Model of the concat demuxer: the ``file`` arguments, in order.

    Lines end at ``\\r``, ``\\n`` or NUL; the keyword is a space-delimited
    word; the ``file`` argument is one ``av_get_token`` with space terminators.
    """
    files: list[str] = []
    line = ""
    lines: list[str] = []
    for ch in text:
        if ch in "\r\n\0":
            lines.append(line)
            line = ""
        else:
            line += ch
    lines.append(line)
    for raw in lines:
        stripped = raw.lstrip(CONCAT_SPACE_CHARS)
        if not stripped or stripped.startswith("#"):
            continue
        keyword_end = 0
        while (
            keyword_end < len(stripped)
            and stripped[keyword_end] not in CONCAT_SPACE_CHARS
        ):
            keyword_end += 1
        keyword = stripped[:keyword_end]
        rest = stripped[keyword_end:].lstrip(CONCAT_SPACE_CHARS)
        if keyword == "file":
            token, _ = av_get_token(rest, CONCAT_SPACE_CHARS)
            files.append(token)
    return files

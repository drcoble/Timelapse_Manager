"""Tiny, dependency-free image-dimension reader.

Every adapter must populate :class:`~.base.CapturedFrame` with pixel width and
height, but the project avoids heavyweight image libraries (Pillow) to stay
lean on small hardware. There is no standard-library call that returns JPEG
dimensions, so this module walks the JPEG marker segments and reads the size out
of the Start-Of-Frame (SOF) header. PNG is handled too because some cameras and
the ONVIF snapshot path can return it.

The parsers are defensive: they never raise on malformed input, returning
``None`` instead, so a caller can fall back gracefully rather than crash a
capture loop on an odd frame.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta, timezone

# JPEG Start-Of-Frame markers whose payload begins with precision then the
# 16-bit height and width. Excludes 0xC4 (DHT), 0xC8 (JPG), 0xCC (DAC), which
# share the 0xCn range but are not frame headers.
_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)

# Markers that stand alone with no length field (besides the 0xFFD8 SOI we skip).
_STANDALONE_MARKERS = frozenset({0xD8, 0xD9} | set(range(0xD0, 0xD8)))

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ImageTooLargeError(ValueError):
    """An image header declares a pixel dimension beyond the allowed ceiling.

    Raised by :func:`read_dimensions_bounded` so a rogue or malfunctioning camera
    that reports an enormous width/height is refused *before* any decode allocates
    a multi-gigabyte pixel buffer. A plain :class:`ValueError`, since the image
    data itself is what is out of range.
    """


def detect_format(data: bytes) -> str | None:
    """Return the lower-case image format name, or None if unrecognised.

    Only the formats the capture pipeline can encounter are detected: ``jpeg``
    and ``png``.
    """
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == _PNG_SIGNATURE:
        return "png"
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Scan JPEG segments for an SOF marker and return ``(width, height)``."""
    # Skip the two-byte SOI (0xFFD8).
    offset = 2
    length = len(data)
    while offset + 1 < length:
        # Markers start with 0xFF; fill bytes (0xFF) may repeat before the code.
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker == 0xFF:
            # Padding/fill; back up one so the next 0xFF is reconsidered.
            offset -= 1
            continue
        if marker in _STANDALONE_MARKERS:
            continue
        if offset + 2 > length:
            return None
        (segment_len,) = struct.unpack(">H", data[offset : offset + 2])
        if segment_len < 2:
            return None
        if marker in _SOF_MARKERS:
            # Payload: 1 byte precision, 2 bytes height, 2 bytes width.
            if offset + 7 > length:
                return None
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return (width, height)
        offset += segment_len
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read ``(width, height)`` from a PNG IHDR chunk."""
    # Signature (8) + chunk length (4) + "IHDR" (4) -> width/height at byte 16.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height)


def read_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` for JPEG or PNG bytes, else None.

    Never raises; malformed or unknown input yields None.
    """
    try:
        fmt = detect_format(data)
        if fmt == "jpeg":
            return _jpeg_dimensions(data)
        if fmt == "png":
            return _png_dimensions(data)
    except (struct.error, IndexError):
        return None
    return None


# A JPEG must end with the End-Of-Image marker. Some encoders pad a few bytes
# after it, so it is looked for within this many trailing bytes, not only at
# the very end.
_JPEG_EOI = b"\xff\xd9"
JPEG_EOI_WINDOW = 16


def has_jpeg_eoi(data: bytes) -> bool:
    """Return whether a JPEG body carries its End-Of-Image marker (``FFD9``).

    Looks within the last :data:`JPEG_EOI_WINDOW` bytes so trailing padding is
    tolerated. A body cut off in transfer (a dropped connection, a short read)
    has no EOI there. Never raises.
    """
    return _JPEG_EOI in data[-JPEG_EOI_WINDOW:]


def frame_integrity_problem(
    data: bytes, fmt: str | None, dimensions: tuple[int, int] | None
) -> str | None:
    """Return why a captured image is unusable, or ``None`` if it looks whole.

    A frame is rejected when its dimensions could not be read, when either axis
    is zero, or when a JPEG has no End-Of-Image marker (a truncated body). The
    result is a short reason for an error message. Pure; never raises.
    """
    if dimensions is None:
        return "no readable dimensions"
    width, height = dimensions
    if width <= 0 or height <= 0:
        return f"zero dimension {width}x{height}"
    if fmt == "jpeg" and not has_jpeg_eoi(data):
        return "JPEG end-of-image marker missing"
    return None


def read_dimensions_bounded(
    data: bytes, max_dimension_px: int
) -> tuple[int, int] | None:
    """Return ``(width, height)`` like :func:`read_dimensions`, but reject giants.

    Reads the dimensions from the image header and, when either the width or the
    height exceeds ``max_dimension_px``, raises :class:`ImageTooLargeError` instead
    of returning them. This is the gate to call before a decode or thumbnail step:
    the header is cheap to parse, so a dimension bomb is refused before any
    full-resolution pixel buffer is allocated.

    Unreadable or unrecognised input still yields ``None`` (the header could not be
    parsed, so there is nothing to bound) -- only a header that positively declares
    an over-ceiling dimension raises.
    """
    dimensions = read_dimensions(data)
    if dimensions is None:
        return None
    width, height = dimensions
    if width > max_dimension_px or height > max_dimension_px:
        raise ImageTooLargeError(
            f"image dimensions {width}x{height} exceed the {max_dimension_px}px ceiling"
        )
    return dimensions


# TIFF/Exif tag numbers and field type used by the capture-timestamp reader.
_TAG_DATETIME = 0x0132  # IFD0 DateTime (fallback)
_TAG_EXIF_IFD_POINTER = 0x8769  # IFD0 -> offset of the Exif sub-IFD
_TAG_DATETIME_ORIGINAL = 0x9003  # Exif sub-IFD DateTimeOriginal (preferred)
_TAG_OFFSET_TIME = 0x9010  # Exif sub-IFD OffsetTime (zone of DateTime)
_TAG_OFFSET_TIME_ORIGINAL = 0x9011  # Exif sub-IFD OffsetTimeOriginal
_DATETIME_TAGS = (
    _TAG_DATETIME,
    _TAG_DATETIME_ORIGINAL,
    _TAG_OFFSET_TIME,
    _TAG_OFFSET_TIME_ORIGINAL,
)
_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"  # "YYYY:MM:DD HH:MM:SS"
# Exif offset text: a sign, two-digit hours and minutes ("+02:00"). The range is
# limited to the real-world offsets (-12:00 .. +14:00).
_EXIF_OFFSET_PATTERN = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_MAX_OFFSET_MINUTES_EAST = 14 * 60
_MAX_OFFSET_MINUTES_WEST = 12 * 60


def _find_exif_segment(data: bytes) -> bytes | None:
    """Return the TIFF payload of the JPEG APP1/Exif segment, or None.

    Walks the JPEG marker segments looking for an APP1 marker (``0xFFE1``) whose
    payload begins with the ``b"Exif\\x00\\x00"`` identifier, and returns the
    bytes that follow it (the embedded TIFF structure). APP1 segments that hold
    something else (e.g. an XMP packet) are skipped so the scan keeps going.
    Returns None when no Exif segment is present.
    """
    offset = 2  # skip the two-byte SOI (0xFFD8)
    length = len(data)
    while offset + 1 < length:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker == 0xFF:
            offset -= 1  # padding/fill; reconsider the next 0xFF
            continue
        if marker in _STANDALONE_MARKERS:
            continue
        if marker == 0xDA:
            # Start of scan: image data follows, no more metadata segments.
            return None
        if offset + 2 > length:
            return None
        (segment_len,) = struct.unpack(">H", data[offset : offset + 2])
        if segment_len < 2:
            return None
        payload_start = offset + 2
        payload_end = offset + segment_len
        if marker == 0xE1:  # APP1
            payload = data[payload_start:payload_end]
            if payload[:6] == b"Exif\x00\x00":
                return payload[6:]
        offset += segment_len
    return None


def _read_ifd_datetime_tags(
    tiff: bytes, ifd_offset: int, byte_order: str
) -> tuple[dict[int, str], int | None]:
    """Read an IFD's date tags and the Exif sub-IFD pointer.

    Parses the IFD at ``ifd_offset`` (relative to the TIFF header start) for this
    module's tags of interest, resolving each ASCII value through its offset --
    the value lives at ``tiff[value_offset:...]`` because a 20-byte timestamp
    cannot fit in the entry's inline 4-byte value field. Returns a mapping of the
    found ``{tag: text}`` plus the offset of the Exif sub-IFD when its pointer tag
    is present (else None). ``byte_order`` is ``"<"`` or ``">"``.
    """
    found: dict[int, str] = {}
    exif_ifd_offset: int | None = None
    (entry_count,) = struct.unpack(byte_order + "H", tiff[ifd_offset : ifd_offset + 2])
    for i in range(entry_count):
        entry = ifd_offset + 2 + i * 12
        tag, field_type, count = struct.unpack(
            byte_order + "HHI", tiff[entry : entry + 8]
        )
        value_field = tiff[entry + 8 : entry + 12]
        if tag == _TAG_EXIF_IFD_POINTER and field_type == 4:  # LONG
            (exif_ifd_offset,) = struct.unpack(byte_order + "I", value_field)
            continue
        if tag in _DATETIME_TAGS and field_type == 2:  # ASCII
            # ASCII values longer than 4 bytes are stored at an offset; shorter
            # ones sit inline in the entry's value field.
            if count > 4:
                (value_offset,) = struct.unpack(byte_order + "I", value_field)
                raw = tiff[value_offset : value_offset + count]
            else:
                raw = value_field[:count]
            text = raw.split(b"\x00", 1)[0].decode("ascii", "strict")
            found[tag] = text
    return found, exif_ifd_offset


def _parse_exif_offset(text: str | None) -> timezone | None:
    """Return the fixed zone for an Exif offset string such as ``"+02:00"``.

    Anything else -- missing, blank (cameras write spaces when the zone is
    unknown), malformed or out of the real-world range -- yields None, so the
    caller treats the time as having no zone.
    """
    if text is None:
        return None
    match = _EXIF_OFFSET_PATTERN.match(text.strip())
    if match is None:
        return None
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    if int(match.group(3)) > 59:
        return None
    if match.group(1) == "-":
        if minutes > _MAX_OFFSET_MINUTES_WEST:
            return None
        minutes = -minutes
    elif minutes > _MAX_OFFSET_MINUTES_EAST:
        return None
    return timezone(timedelta(minutes=minutes))


def read_capture_timestamp(data: bytes) -> datetime | None:
    """Return the JPEG Exif capture time, or None.

    Reads the embedded TIFF structure of the JPEG APP1/Exif segment without any
    image-library dependency, mirroring this module's hand-parsed,
    never-raise contract. Tag ``0x9003`` (``DateTimeOriginal``, in the Exif
    sub-IFD) is preferred; tag ``0x0132`` (``DateTime``, in IFD0) is the
    fallback. Both TIFF byte orders are handled.

    Exif stores a local wall-clock time. When the image also carries a valid
    UTC offset -- ``OffsetTimeOriginal`` (``0x9011``) for ``DateTimeOriginal``,
    else ``OffsetTime`` (``0x9010``) -- the result is **timezone-aware** at that
    fixed offset. Without one it is **naive** (no tzinfo): the caller decides
    which zone the wall-clock time was in (the column convention treats a naive
    value as UTC). A missing or unreadable timestamp yields None so the caller
    can fall back to a supplied time. Never raises -- any malformed input
    returns None.
    """
    try:
        if detect_format(data) != "jpeg":
            return None
        tiff = _find_exif_segment(data)
        if tiff is None:
            return None
        order_mark = tiff[:2]
        if order_mark == b"II":
            byte_order = "<"
        elif order_mark == b"MM":
            byte_order = ">"
        else:
            return None
        (magic,) = struct.unpack(byte_order + "H", tiff[2:4])
        if magic != 0x002A:
            return None
        (ifd0_offset,) = struct.unpack(byte_order + "I", tiff[4:8])

        ifd0_tags, exif_ifd_offset = _read_ifd_datetime_tags(
            tiff, ifd0_offset, byte_order
        )
        exif_tags: dict[int, str] = {}
        text: str | None = None
        offset_text: str | None = None
        if exif_ifd_offset is not None:
            exif_tags, _ = _read_ifd_datetime_tags(tiff, exif_ifd_offset, byte_order)
            text = exif_tags.get(_TAG_DATETIME_ORIGINAL)
            offset_text = exif_tags.get(_TAG_OFFSET_TIME_ORIGINAL)
        if text is None:
            text = ifd0_tags.get(_TAG_DATETIME)
        if text is None:
            return None
        if offset_text is None:
            offset_text = exif_tags.get(_TAG_OFFSET_TIME)
        parsed = datetime.strptime(text, _EXIF_DATETIME_FORMAT)
        zone = _parse_exif_offset(offset_text)
        return parsed if zone is None else parsed.replace(tzinfo=zone)
    except (struct.error, ValueError, IndexError, UnicodeDecodeError):
        return None

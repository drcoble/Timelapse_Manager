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

import struct
from datetime import datetime

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


# TIFF/Exif tag numbers and field type used by the capture-timestamp reader.
_TAG_DATETIME = 0x0132  # IFD0 DateTime (fallback)
_TAG_EXIF_IFD_POINTER = 0x8769  # IFD0 -> offset of the Exif sub-IFD
_TAG_DATETIME_ORIGINAL = 0x9003  # Exif sub-IFD DateTimeOriginal (preferred)
_EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"  # "YYYY:MM:DD HH:MM:SS"


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
        if tag in (_TAG_DATETIME, _TAG_DATETIME_ORIGINAL) and field_type == 2:  # ASCII
            # ASCII values longer than 4 bytes are stored at an offset.
            (value_offset,) = struct.unpack(byte_order + "I", value_field)
            raw = tiff[value_offset : value_offset + count]
            text = raw.split(b"\x00", 1)[0].decode("ascii", "strict")
            found[tag] = text
    return found, exif_ifd_offset


def read_capture_timestamp(data: bytes) -> datetime | None:
    """Return the JPEG Exif capture time as a naive datetime, or None.

    Reads the embedded TIFF structure of the JPEG APP1/Exif segment without any
    image-library dependency, mirroring this module's hand-parsed,
    never-raise contract. Tag ``0x9003`` (``DateTimeOriginal``, in the Exif
    sub-IFD) is preferred; tag ``0x0132`` (``DateTime``, in IFD0) is the
    fallback. Both TIFF byte orders are handled.

    The returned value is **naive** (no tzinfo): Exif stores a local wall-clock
    time with no zone, and the column convention treats naive timestamps as UTC.
    The value is therefore best-effort and is surfaced to the user as editable; a
    missing or unreadable timestamp yields None so the caller can fall back to a
    supplied time. Never raises -- any malformed input returns None.
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
        text: str | None = None
        if exif_ifd_offset is not None:
            exif_tags, _ = _read_ifd_datetime_tags(tiff, exif_ifd_offset, byte_order)
            text = exif_tags.get(_TAG_DATETIME_ORIGINAL)
        if text is None:
            text = ifd0_tags.get(_TAG_DATETIME)
        if text is None:
            return None
        return datetime.strptime(text, _EXIF_DATETIME_FORMAT)
    except (struct.error, ValueError, IndexError, UnicodeDecodeError):
        return None

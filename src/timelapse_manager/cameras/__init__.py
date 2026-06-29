"""Camera adapters for the supported network camera protocols.

The public surface is the protocol-agnostic seam: the :class:`CameraAdapter`
abstract base class, the typed result/error vocabulary, the :func:`build_adapter`
factory, and the discovery and geolocation entry points. Consumers (the capture
engine) depend only on these names, never on a concrete adapter class.
"""

from __future__ import annotations

from .base import (
    AuthCaptureError,
    CameraAdapter,
    CameraCapabilities,
    CapturedFrame,
    CaptureError,
    DiscoveredCamera,
    GeoLocation,
    OtherCaptureError,
    StreamProfile,
    StreamProfileResult,
    TimeoutCaptureError,
    UnreachableCaptureError,
    UnsupportedCodecCaptureError,
    ValidationFailure,
    ValidationResult,
)
from .discovery import (
    InvalidScanRange,
    ScanRangeTooLarge,
    check_scan_range,
    count_hosts,
    discover_onvif,
    resolve_discovered_uris,
    scan_range,
)
from .geolocation import get_camera_geolocation
from .host_resolution import resolve_camera_host
from .http_jpeg import HttpJpegAdapter
from .onvif import OnvifAdapter
from .probing import (
    Confidence,
    DetectionOutcome,
    ProtocolCandidate,
    detect_protocols,
)
from .registry import build_adapter, effective_credentials
from .rtsp import RtspAdapter
from .vapix import VapixAdapter

__all__ = [
    # Seam: base interface and value/error types.
    "CameraAdapter",
    "CapturedFrame",
    "CameraCapabilities",
    "GeoLocation",
    "ValidationResult",
    "ValidationFailure",
    "DiscoveredCamera",
    "StreamProfile",
    "StreamProfileResult",
    # Errors.
    "CaptureError",
    "AuthCaptureError",
    "UnreachableCaptureError",
    "TimeoutCaptureError",
    "UnsupportedCodecCaptureError",
    "OtherCaptureError",
    # Concrete adapters.
    "HttpJpegAdapter",
    "RtspAdapter",
    "VapixAdapter",
    "OnvifAdapter",
    # Factory, discovery, geolocation, and the SSRF seam.
    "build_adapter",
    "effective_credentials",
    "discover_onvif",
    "scan_range",
    "check_scan_range",
    "count_hosts",
    "InvalidScanRange",
    "ScanRangeTooLarge",
    "resolve_discovered_uris",
    "get_camera_geolocation",
    "resolve_camera_host",
    # Protocol detection.
    "detect_protocols",
    "DetectionOutcome",
    "ProtocolCandidate",
    "Confidence",
]

"""The RTSP ffmpeg argv constrains the input to RTSP transport protocols.

A hostile stream's SDP can try to steer ffmpeg into ``file:``, ``concat:`` or
``http:`` protocols to read local files or reach other hosts. The capture argv
pins ``-protocol_whitelist`` among the input options (before ``-i``) to close
that hole; these tests guard that placement.
"""

from __future__ import annotations

from timelapse_manager.cameras.rtsp import build_ffmpeg_command


def test_protocol_whitelist_present() -> None:
    argv = build_ffmpeg_command("rtsp://camera.example/stream")
    assert "-protocol_whitelist" in argv
    flag_index = argv.index("-protocol_whitelist")
    assert argv[flag_index + 1] == "rtp,rtsp,tcp,udp"


def test_protocol_whitelist_precedes_input() -> None:
    argv = build_ffmpeg_command("rtsp://camera.example/stream")
    # The whitelist must be an *input* option, i.e. before the -i it guards.
    assert argv.index("-protocol_whitelist") < argv.index("-i")


def test_whitelist_does_not_displace_transport_or_timeout() -> None:
    argv = build_ffmpeg_command(
        "rtsp://camera.example/stream", transport="udp", timeout_seconds=15.0
    )
    # Existing input flags are still present and still precede -i.
    assert argv[argv.index("-rtsp_transport") + 1] == "udp"
    assert argv[argv.index("-timeout") + 1] == str(int(15.0 * 1_000_000))
    assert argv.index("-rtsp_transport") < argv.index("-i")
    assert argv.index("-timeout") < argv.index("-i")
    assert argv[argv.index("-i") + 1] == "rtsp://camera.example/stream"

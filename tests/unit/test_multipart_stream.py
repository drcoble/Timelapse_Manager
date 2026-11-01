"""The streaming multipart reader (#67 P1): parser, spool and limits."""

from __future__ import annotations

import asyncio
import random
import threading
import tracemalloc
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from timelapse_manager.executors import run_in
from timelapse_manager.web.multipart import (
    BodyTooLargeError,
    FormEnd,
    InsufficientSpoolSpaceError,
    MultipartError,
    MultipartParser,
    PartData,
    PartEnd,
    PartStart,
    SpoolReleasedError,
    TooManyFilesError,
    UploadSpool,
    _SpoolFile,
    parse_multipart,
    stream_multipart,
    upload_spool,
)

_B = b"XyZ-boundary-42"
_CT = f"multipart/form-data; boundary={_B.decode()}"
_MiB = 1024 * 1024


def _body(
    parts: list[tuple[str, str | None, bytes]], *, preamble: bytes = b""
) -> bytes:
    """Build a multipart body from ``(name, filename-or-None, content)`` parts."""
    out = preamble
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += b"--" + _B + b"\r\nContent-Disposition: " + disposition.encode()
        if filename is not None:
            out += b"\r\nContent-Type: image/jpeg"
        out += b"\r\n\r\n" + content + b"\r\n"
    return out + b"--" + _B + b"--\r\n"


def _events(body: bytes, sizes: Iterator[int]) -> list[object]:
    """Run ``body`` through a parser in chunks of the given sizes; merge data."""
    parser = MultipartParser(_B)
    merged: list[object] = []
    position = 0
    while position < len(body):
        size = next(sizes)
        for event in parser.feed(body[position : position + size]):
            if (
                isinstance(event, PartData)
                and merged
                and isinstance(merged[-1], PartData)
            ):
                merged[-1] = PartData(merged[-1].data + event.data)
            else:
                merged.append(event)
        position += size
    parser.finish()
    return merged


def _request(body: bytes | AsyncIterator[bytes], content_type: str = _CT) -> Request:
    """A Request whose body arrives in the given chunks (bytes: one chunk)."""
    chunks: AsyncIterator[bytes]
    if isinstance(body, bytes):

        async def single() -> AsyncIterator[bytes]:
            yield body

        chunks = single()
    else:
        chunks = body

    async def receive() -> dict[str, object]:
        try:
            return {
                "type": "http.request",
                "body": await chunks.__anext__(),
                "more_body": True,
            }
        except StopAsyncIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(b"content-type", content_type.encode())],
    }
    return Request(scope, receive)


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


def _stream(request: Request, spool: Path, **overrides: int):  # noqa: ANN202
    limits: dict[str, int] = {
        "max_file_bytes": 10 * _MiB,
        "max_total_bytes": 20 * _MiB,
        "max_files": 10,
    }
    limits.update(overrides)
    return stream_multipart(
        request,
        spool_dir=spool,
        file_field="files",
        text_fields=frozenset({"csrf_token"}),
        **limits,
    )


class TestParserChunking:
    """The events for a body do not depend on how it was split."""

    @pytest.mark.parametrize("size", [1, 2, 3, 7, 16, 17, 64, 10_000])
    def test_fixed_chunk_sizes_agree(self, size: int) -> None:
        # Content that contains partial delimiters at every alignment.
        tricky = b"\r\n--Xy\r\n--XyZ-boundary-4\r\n-\r\n" * 5 + bytes(range(256))
        body = _body(
            [
                ("csrf_token", None, b"tok"),
                ("files", "a.jpg", tricky),
                ("files", "b.jpg", b""),
                ("note", None, "héllo".encode()),
            ]
        )
        whole = _events(body, iter([len(body)]))
        assert _events(body, iter([size] * len(body))) == whole
        starts = [e for e in whole if isinstance(e, PartStart)]
        assert [s.name for s in starts] == ["csrf_token", "files", "files", "note"]
        data = [e.data for e in whole if isinstance(e, PartData)]
        assert tricky in data

    def test_every_split_point_of_a_small_body(self) -> None:
        body = _body([("csrf_token", None, b"tok"), ("files", "a.jpg", b"\x00" * 40)])
        whole = _events(body, iter([len(body)]))
        for cut in range(1, len(body)):
            assert _events(body, iter([cut, len(body)])) == whole

    def test_random_chunking_matches_the_in_memory_parser(self) -> None:
        rng = random.Random(67)
        for _ in range(200):
            payload = rng.randbytes(rng.randrange(0, 600))
            body = _body(
                [
                    ("csrf_token", None, b"t"),
                    ("files", "x.jpg", payload),
                    ("files", "y.jpg", payload[::-1]),
                ]
            )
            sizes = iter(lambda: rng.randrange(1, 90), None)
            events = _events(body, sizes)
            expected = parse_multipart(body, _CT)
            files = [b"".join(e.data for e in group) for group in _file_groups(events)]
            assert files == [f.data for f in expected.files]


def _file_groups(events: list[object]) -> list[list[PartData]]:
    groups: list[list[PartData]] = []
    current: list[PartData] | None = None
    for event in events:
        if isinstance(event, PartStart):
            current = [] if event.filename else None
        elif isinstance(event, PartData) and current is not None:
            current.append(event)
        elif isinstance(event, PartEnd) and current is not None:
            if current:
                groups.append(current)
            current = None
    return groups


class TestParserStrictness:
    def test_preamble_is_dropped(self) -> None:
        body = _body([("a", None, b"1")], preamble=b"ignored preamble\r\n")
        events = _events(body, iter([5] * len(body)))
        assert isinstance(events[0], PartStart)
        assert isinstance(events[-1], FormEnd)

    def test_truncated_body_is_an_error(self) -> None:
        body = _body([("a", None, b"1")])[:-8]
        with pytest.raises(MultipartError):
            _events(body, iter([len(body)]))

    def test_boundary_followed_by_junk_is_an_error(self) -> None:
        parser = MultipartParser(_B)
        with pytest.raises(MultipartError):
            parser.feed(b"--" + _B + b"junk\r\n")

    def test_oversized_header_block_is_an_error(self) -> None:
        parser = MultipartParser(_B)
        with pytest.raises(MultipartError):
            parser.feed(b"--" + _B + b"\r\nX: " + b"a" * 20_000)

    def test_oversized_text_field_is_an_error(self) -> None:
        parser = MultipartParser(_B)
        with pytest.raises(MultipartError):
            parser.feed(_body([("a", None, b"x" * 70_000)]))

    def test_oversized_preamble_is_an_error(self) -> None:
        parser = MultipartParser(_B)
        with pytest.raises(MultipartError):
            parser.feed(b"a" * 200_000)

    def test_unnamed_part_has_an_empty_name(self) -> None:
        body = (
            b"--" + _B + b"\r\nContent-Type: text/plain\r\n\r\nx\r\n--" + _B + b"--\r\n"
        )
        events = _events(body, iter([len(body)]))
        assert [e for e in events if isinstance(e, PartStart)][0].name == ""


class TestStreamMultipart:
    def test_spools_files_and_keeps_text_fields(self, tmp_path: Path) -> None:
        body = _body(
            [
                ("csrf_token", None, b"tok"),
                ("files", "a.jpg", b"AAA"),
                ("files", "empty.jpg", b""),
                ("files", "", b"no-name"),
                ("other", "o.jpg", b"not-spooled"),
                ("files", "b.jpg", b"BBBB"),
            ]
        )
        form = _run(_stream(_request(body), tmp_path))
        assert form.fields == {"csrf_token": "tok"}
        assert [(f.filename, f.size) for f in form.files] == [
            ("a.jpg", 3),
            ("b.jpg", 4),
        ]
        assert [f.path.read_bytes() for f in form.files if f.path] == [b"AAA", b"BBBB"]
        # Nothing else was left on disk (empty and unnamed parts are removed).
        assert len(list(tmp_path.iterdir())) == 2

    def test_filename_is_never_a_path(self, tmp_path: Path) -> None:
        spool = tmp_path / "spool"
        spool.mkdir()
        evil = "../../escape.jpg"
        form = _run(_stream(_request(_body([("files", evil, b"x")])), spool))
        (part,) = form.files
        assert part.filename == evil  # only a label
        assert part.path is not None and part.path.parent == spool
        assert not (tmp_path.parent / "escape.jpg").exists()
        assert not (tmp_path / "escape.jpg").exists()

    def test_per_file_cap_is_enforced_while_streaming(self, tmp_path: Path) -> None:
        pulled = 0

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal pulled
            yield _body([("files", "big.jpg", b"")])[: -len(b"--" + _B + b"--\r\n") - 2]
            while True:
                pulled += 1
                yield b"x" * 4096

        with pytest.raises(BodyTooLargeError):
            _run(_stream(_request(chunks()), tmp_path, max_file_bytes=20_000))
        # Cut off after about the cap, not after buffering the endless body.
        assert pulled <= 6

    def test_total_cap(self, tmp_path: Path) -> None:
        body = _body([("files", "a.jpg", b"x" * 5000)])
        with pytest.raises(BodyTooLargeError):
            _run(_stream(_request(body), tmp_path, max_total_bytes=1000))

    def test_file_count_cap(self, tmp_path: Path) -> None:
        body = _body([("files", f"{i}.jpg", b"x") for i in range(4)])
        with pytest.raises(TooManyFilesError):
            _run(_stream(_request(body), tmp_path, max_files=3))
        # Exactly at the cap is accepted.
        _run(_stream(_request(body), tmp_path, max_files=4))

    def test_missing_boundary_and_truncated_body(self, tmp_path: Path) -> None:
        with pytest.raises(MultipartError):
            _run(_stream(_request(b"x", "multipart/form-data"), tmp_path))
        with pytest.raises(MultipartError):
            _run(_stream(_request(_body([("files", "a", b"x")])[:-10]), tmp_path))

    def test_200_mb_body_peak_memory_is_bounded(self, tmp_path: Path) -> None:
        """A 200 MB upload never sits in memory: peak stays far below its size."""
        max_file = 64 * _MiB
        file_count = 4
        per_file = 50 * _MiB
        chunk = b"\xab" * _MiB

        async def chunks() -> AsyncIterator[bytes]:
            for i in range(file_count):
                yield (
                    b"--" + _B + b"\r\n"
                    b'Content-Disposition: form-data; name="files"; '
                    + f'filename="{i}.jpg"\r\n\r\n'.encode()
                )
                for _ in range(per_file // _MiB):
                    yield chunk
                yield b"\r\n"
            yield b"--" + _B + b"--\r\n"

        tracemalloc.start()
        try:
            form = _run(
                _stream(
                    _request(chunks()),
                    tmp_path,
                    max_file_bytes=max_file,
                    max_total_bytes=300 * _MiB,
                )
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert [f.size for f in form.files] == [per_file] * file_count
        assert peak < 3 * max_file
        # In fact it is a handful of chunks, not a file.
        assert peak < 16 * _MiB


class TestTextPartLimits:
    """Non-file parts cannot be used to dodge the streaming caps."""

    def _stream_text(self, request: Request, spool: Path):  # noqa: ANN202
        return stream_multipart(
            request,
            spool_dir=spool,
            file_field="files",
            text_fields=frozenset({"csrf_token"}),
            max_file_bytes=_MiB,
            max_total_bytes=20 * _MiB,
            max_files=10,
        )

    def test_only_wanted_fields_are_kept(self, tmp_path: Path) -> None:
        body = _body(
            [
                ("csrf_token", None, b"tok"),
                ("junk", None, b"x" * 1000),
                ("", None, b"unnamed"),
                ("files", "a.jpg", b"A"),
            ]
        )
        form = _run(self._stream_text(_request(body), tmp_path))
        assert form.fields == {"csrf_token": "tok"}
        assert len(form.files) == 1

    def test_one_huge_text_field_is_refused_without_buffering(
        self, tmp_path: Path
    ) -> None:
        pulled = 0

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal pulled
            yield (
                b"--" + _B + b'\r\nContent-Disposition: form-data; name="junk"\r\n\r\n'
            )
            while True:
                pulled += 1
                yield b"z" * 4096

        tracemalloc.start()
        try:
            with pytest.raises(MultipartError):
                _run(self._stream_text(_request(chunks()), tmp_path))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert pulled < 40  # cut off near the 64 KiB field cap
        assert peak < _MiB
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize("name", ["junk", ""], ids=["named", "unnamed"])
    def test_many_small_text_parts_are_refused(self, tmp_path: Path, name: str) -> None:
        body = _body([(name or "", None, b"x") for _ in range(200)])
        if not name:
            body = body.replace(b'name=""', b"")
        with pytest.raises(MultipartError):
            _run(self._stream_text(_request(body), tmp_path))
        assert list(tmp_path.iterdir()) == []

    def test_dropped_file_parts_count_too(self, tmp_path: Path) -> None:
        body = _body([("other", f"{i}.jpg", b"x") for i in range(200)])
        with pytest.raises(MultipartError):
            _run(self._stream_text(_request(body), tmp_path))
        assert list(tmp_path.iterdir()) == []

    def test_a_single_unnamed_text_part_is_dropped(self, tmp_path: Path) -> None:
        body = (
            b"--" + _B + b"\r\nContent-Type: text/plain\r\n\r\nx\r\n--" + _B + b"--\r\n"
        )
        form = _run(self._stream_text(_request(body), tmp_path))
        assert form.fields == {} and form.files == []


class TestEmptyAndLowDisk:
    def test_empty_file_parts_count_and_never_open_a_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#220: 1000 empty ``files`` parts are refused; no file was opened."""
        opened = 0
        real = _SpoolFile.open

        async def counting(self: _SpoolFile) -> None:
            nonlocal opened
            opened += 1
            await real(self)

        monkeypatch.setattr(_SpoolFile, "open", counting)
        body = _body([("files", f"{i}.jpg", b"") for i in range(1000)])
        with pytest.raises(TooManyFilesError):
            _run(_stream(_request(body), tmp_path, max_files=200))
        assert opened <= 200
        assert list(tmp_path.iterdir()) == []

    def test_free_space_dropping_mid_upload_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#219: a chunked body cannot push free space under the watermark."""
        calls = 0

        def usage(path: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            return SimpleNamespace(free=10 * _MiB if calls <= 1 else 0)

        monkeypatch.setattr("timelapse_manager.web.multipart.shutil.disk_usage", usage)

        async def chunks() -> AsyncIterator[bytes]:
            yield (
                b"--"
                + _B
                + b'\r\nContent-Disposition: form-data; name="files"; '
                + b'filename="a"\r\n\r\n'
            )
            for _ in range(5):
                yield b"x" * _MiB
            yield b"\r\n--" + _B + b"--\r\n"

        with pytest.raises(InsufficientSpoolSpaceError):
            _run(
                stream_multipart(
                    _request(chunks()),
                    spool_dir=tmp_path,
                    file_field="files",
                    max_file_bytes=10 * _MiB,
                    max_total_bytes=20 * _MiB,
                    max_files=10,
                    low_watermark_bytes=_MiB,
                )
            )
        assert calls >= 2


class TestUploadSpool:
    def test_removed_on_success_and_on_error(self, tmp_path: Path) -> None:
        async def go(fail: bool) -> Path:
            async with upload_spool(
                tmp_path, expected_bytes=0, low_watermark_bytes=0
            ) as spool:
                (spool.path / "f").write_bytes(b"x")
                path = spool.path
                if fail:
                    raise RuntimeError("boom")
            return path

        path = _run(go(False))
        assert not path.exists()
        with pytest.raises(RuntimeError):
            _run(go(True))
        assert list((tmp_path / "tmp").iterdir()) == []

    def test_removed_on_cancellation(self, tmp_path: Path) -> None:
        seen: list[Path] = []

        async def go() -> None:
            async with upload_spool(
                tmp_path, expected_bytes=0, low_watermark_bytes=0
            ) as spool:
                seen.append(spool.path)
                await asyncio.sleep(30)

        async def cancel() -> None:
            task = asyncio.create_task(go())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        _run(cancel())
        assert seen and not seen[0].exists()

    def test_a_claimed_spool_is_left_to_the_worker(self, tmp_path: Path) -> None:
        async def go() -> UploadSpool:
            async with upload_spool(
                tmp_path, expected_bytes=0, low_watermark_bytes=0
            ) as spool:
                assert spool.claim()
                return spool

        spool = _run(go())
        assert spool.path.exists()
        spool.finish()
        assert not spool.path.exists()

    def test_a_job_that_never_starts_leaves_nothing_when_cancelled(
        self, tmp_path: Path
    ) -> None:
        """#218: the request is cancelled while its job is still queued."""
        gate = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)
        ran: list[str] = []

        def job(spool: UploadSpool) -> None:
            spool.claim_or_raise()
            ran.append("ran")
            spool.finish()

        async def go() -> list[UploadSpool]:
            seen: list[UploadSpool] = []
            executor.submit(gate.wait)  # occupies the only worker

            async def request() -> None:
                async with upload_spool(
                    tmp_path, expected_bytes=0, low_watermark_bytes=0
                ) as spool:
                    seen.append(spool)
                    await run_in(executor, job, spool)

            task = asyncio.create_task(request())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return seen

        try:
            (spool,) = _run(go())
            assert not spool.path.exists()
            assert list((tmp_path / "tmp").iterdir()) == []
            gate.set()
            executor.shutdown(wait=True)
            assert ran == []  # the late-starting job found the spool released
        finally:
            gate.set()

    def test_executor_shutdown_cancelling_the_job_leaves_nothing(
        self, tmp_path: Path
    ) -> None:
        gate = threading.Event()
        executor = ThreadPoolExecutor(max_workers=1)

        async def go() -> None:
            executor.submit(gate.wait)

            async def request() -> None:
                async with upload_spool(
                    tmp_path, expected_bytes=0, low_watermark_bytes=0
                ) as spool:
                    await run_in(executor, spool.claim_or_raise)

            task = asyncio.create_task(request())
            await asyncio.sleep(0.2)
            executor.shutdown(wait=False, cancel_futures=True)
            with pytest.raises(asyncio.CancelledError):
                await task

        try:
            _run(go())
            assert list((tmp_path / "tmp").iterdir()) == []
        finally:
            gate.set()

    def test_a_worker_after_release_is_refused(self, tmp_path: Path) -> None:
        spool = UploadSpool(tmp_path)
        spool.release()
        with pytest.raises(SpoolReleasedError):
            spool.claim_or_raise()

    def test_low_disk_is_refused(self, tmp_path: Path) -> None:
        from timelapse_manager.web.multipart import InsufficientSpoolSpaceError

        async def go() -> None:
            async with upload_spool(
                tmp_path, expected_bytes=0, low_watermark_bytes=1 << 62
            ):
                pytest.fail("must not enter")

        with pytest.raises(InsufficientSpoolSpaceError):
            _run(go())
        assert list((tmp_path / "tmp").iterdir()) == []

    def test_stale_leftovers_are_swept(self, tmp_path: Path) -> None:
        import os

        root = tmp_path / "tmp"
        old = root / ("upload-" + "a" * 32)
        fresh = root / ("upload-" + "b" * 32)
        other = root / "keep-me"
        for d in (old, fresh, other):
            d.mkdir(parents=True)
            (d / "f").write_bytes(b"x")
        long_ago = 1_000_000_000
        os.utime(old, (long_ago, long_ago))
        os.utime(other, (long_ago, long_ago))

        async def go() -> None:
            async with upload_spool(tmp_path, expected_bytes=0, low_watermark_bytes=0):
                pass

        _run(go())
        assert not old.exists()
        assert fresh.exists() and other.exists()

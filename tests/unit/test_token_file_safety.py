"""Token file safety, caching and rotation (#67 S2).

Covers the service-side create-or-read routine (``O_EXCL`` creation at 0600,
concurrent creators agreeing on one token, loose modes tightened, foreign owners
and symlinks refused), the stat-revalidated cache that lets a rotation take
effect without a restart, and ``rotate_token_file``.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from timelapse_manager.security import token as token_module
from timelapse_manager.security.token import (
    TokenFileCache,
    TokenFileError,
    ensure_token_file,
    read_token_file,
    rotate_token_file,
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX modes and ownership"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix_only
def test_created_file_is_0600_under_a_permissive_umask(tmp_path: Path) -> None:
    path = tmp_path / "data" / ".local-token"
    old = os.umask(0o022)
    try:
        token = ensure_token_file(path)
    finally:
        os.umask(old)
    assert _mode(path) == 0o600
    assert path.read_text(encoding="utf-8") == token
    assert len(token) == 64


def test_no_temp_files_are_left_behind(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    ensure_token_file(path)
    ensure_token_file(path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".local-token"]


def test_concurrent_creators_agree_on_one_token(tmp_path: Path) -> None:
    """Racing first-time creators all return the token that ended up on disk."""
    path = tmp_path / ".local-token"
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def create() -> None:
        barrier.wait()
        value = ensure_token_file(path)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert set(results) == {path.read_text(encoding="utf-8")}
    assert sorted(p.name for p in tmp_path.iterdir()) == [".local-token"]


def test_lost_link_race_reads_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the name appears between the read and the link, the winner's token wins."""
    path = tmp_path / ".local-token"
    real_link = os.link

    def link_after_rival(
        src: str | os.PathLike[str], dst: str | os.PathLike[str]
    ) -> None:
        Path(dst).write_text("rival-token", encoding="utf-8")
        os.chmod(dst, 0o600)
        real_link(src, dst)  # raises FileExistsError, as a real race would

    monkeypatch.setattr(token_module.os, "link", link_after_rival)
    assert ensure_token_file(path) == "rival-token"
    assert sorted(p.name for p in tmp_path.iterdir()) == [".local-token"]


def test_no_hard_link_support_falls_back_to_exclusive_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".local-token"

    def no_links(*_args: object) -> None:
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(token_module.os, "link", no_links)
    token = ensure_token_file(path)
    assert path.read_text(encoding="utf-8") == token
    assert sorted(p.name for p in tmp_path.iterdir()) == [".local-token"]


def test_existing_token_is_returned_unchanged(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    path.write_text("  existing-token\n", encoding="utf-8")
    path.chmod(0o600)
    assert ensure_token_file(path) == "existing-token"
    assert path.read_text(encoding="utf-8") == "  existing-token\n"


@posix_only
def test_loose_mode_is_tightened(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / ".local-token"
    path.write_text("existing-token", encoding="utf-8")
    path.chmod(0o644)
    with caplog.at_level("WARNING", logger=token_module.__name__):
        assert ensure_token_file(path) == "existing-token"
    assert _mode(path) == 0o600
    assert "loose permissions" in caplog.text
    assert "existing-token" not in caplog.text


@posix_only
def test_wrong_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".local-token"
    path.write_text("someone-elses-token", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(token_module, "_current_uid", lambda: os.getuid() + 1)
    with pytest.raises(TokenFileError) as excinfo:
        ensure_token_file(path)
    assert excinfo.value.wrong_owner
    assert "someone-elses-token" not in str(excinfo.value)


@posix_only
def test_symlinked_token_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("linked-token", encoding="utf-8")
    target.chmod(0o600)
    path = tmp_path / ".local-token"
    path.symlink_to(target)
    with pytest.raises(TokenFileError):
        ensure_token_file(path)


def test_empty_token_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    path.write_text("\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(TokenFileError):
        ensure_token_file(path)


# ---------------------------------------------------------------------------
# Client read (the CLI)
# ---------------------------------------------------------------------------


def test_client_read_never_creates(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    with pytest.raises(FileNotFoundError):
        read_token_file(path)
    assert not path.exists()


@posix_only
def test_client_read_skips_owner_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root reading a service-owned file is the normal CLI case."""
    path = tmp_path / ".local-token"
    path.write_text("service-token", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(token_module, "_current_uid", lambda: os.getuid() + 1)
    assert read_token_file(path) == "service-token"


@posix_only
def test_client_read_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("linked-token", encoding="utf-8")
    path = tmp_path / ".local-token"
    path.symlink_to(target)
    with pytest.raises(TokenFileError):
        read_token_file(path)


@posix_only
def test_client_read_refuses_a_directory(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    path.mkdir()
    with pytest.raises((TokenFileError, IsADirectoryError)):
        read_token_file(path)


@posix_only
def test_root_client_read_refuses_loose_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".local-token"
    path.write_text("service-token", encoding="utf-8")
    path.chmod(0o644)
    monkeypatch.setattr(token_module, "_current_uid", lambda: 0)
    with pytest.raises(TokenFileError, match="loose permissions"):
        read_token_file(path)


@posix_only
def test_root_client_read_requires_the_directory_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".local-token"
    path.write_text("service-token", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(token_module, "_current_uid", lambda: 0)
    assert read_token_file(path) == "service-token"
    monkeypatch.setattr(token_module, "_dir_owner", lambda _d: os.getuid() + 1)
    with pytest.raises(TokenFileError, match="owner of its directory"):
        read_token_file(path)


def test_client_read_is_capped(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    path.write_text("a" * 10_000, encoding="utf-8")
    path.chmod(0o600)
    assert len(read_token_file(path)) == 4096


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_reads_once_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".local-token"
    ensure_token_file(path)
    cache = TokenFileCache(path)
    first = cache.get()

    reads = 0
    real_read = token_module._read_validated

    def counting_read(p: Path) -> tuple[str, tuple[int, int, int, int]]:
        nonlocal reads
        reads += 1
        return real_read(p)

    monkeypatch.setattr(token_module, "_read_validated", counting_read)
    for _ in range(5):
        assert cache.get() == first
    assert reads == 0


def test_rotation_is_picked_up_by_the_cache(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    ensure_token_file(path)
    cache = TokenFileCache(path)
    before = cache.get()
    rotate_token_file(path)
    after = cache.get()
    assert after != before
    assert after == path.read_text(encoding="utf-8")


def test_deleted_file_is_recreated_with_a_new_token(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    ensure_token_file(path)
    cache = TokenFileCache(path)
    before = cache.get()
    path.unlink()
    after = cache.get()
    assert after != before
    assert path.exists()


@posix_only
def test_cache_refuses_a_file_swapped_to_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".local-token"
    ensure_token_file(path)
    cache = TokenFileCache(path)
    cache.get()
    rotate_token_file(path)  # new inode: the next get() re-reads and re-checks
    monkeypatch.setattr(token_module, "_current_uid", lambda: os.getuid() + 1)
    with pytest.raises(TokenFileError):
        cache.get()


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotate_requires_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    with pytest.raises(FileNotFoundError):
        rotate_token_file(path)
    assert not path.exists()


@posix_only
def test_rotate_keeps_mode_0600_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / ".local-token"
    before = ensure_token_file(path)
    rotate_token_file(path)
    assert path.read_text(encoding="utf-8") != before
    assert _mode(path) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == [".local-token"]


@posix_only
def test_rotate_as_root_keeps_the_owner_via_the_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner is set with fchown on the open temp fd, never chown by path."""
    path = tmp_path / ".local-token"
    ensure_token_file(path)
    fchowns: list[tuple[int, int]] = []
    monkeypatch.setattr(token_module, "_current_uid", lambda: 0)
    monkeypatch.setattr(
        token_module.os, "fchown", lambda _fd, uid, gid: fchowns.append((uid, gid))
    )

    def no_path_chown(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("chown by path follows symlinks")

    monkeypatch.setattr(token_module.os, "chown", no_path_chown)
    rotate_token_file(path)
    st = path.stat()
    assert fchowns == [(st.st_uid, st.st_gid)]


@posix_only
def test_rotate_temp_file_is_never_a_planted_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-planted name at the temp path makes rotation fail, not follow it."""
    path = tmp_path / ".local-token"
    before = ensure_token_file(path)
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    (tmp_path / "..local-token.fixed.tmp").symlink_to(victim)
    monkeypatch.setattr(token_module.secrets, "token_hex", lambda _n=None: "fixed")
    with pytest.raises(FileExistsError):
        rotate_token_file(path)
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert path.read_text(encoding="utf-8") == before


@posix_only
def test_rotate_refuses_another_accounts_file_when_not_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".local-token"
    before = ensure_token_file(path)
    monkeypatch.setattr(token_module, "_current_uid", lambda: os.getuid() + 1)
    with pytest.raises(TokenFileError):
        rotate_token_file(path)
    assert path.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [".local-token"]

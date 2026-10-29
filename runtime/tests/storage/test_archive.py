# SPDX-License-Identifier: MIT
"""Bounded archive tests (dispatch §required-behavior 5).

Ported from `old-ach-agent/tests/sandbox/test_archive.py`; adds member-count-limit,
special-file-rejection and cancellation-cleanup cases the contract newly requires.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import tarfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from ach_runtime.storage.archive import ArchiveTooLarge, _preflight, extract, pack, write_capped

_NO_MEMBER_CAP = 10_000


def test_pack_extract_roundtrip(tmp_path) -> None:  # noqa: ANN001
    src = tmp_path / "home"
    (src / "workspace" / "repo").mkdir(parents=True)
    (src / "workspace" / "repo" / "f.txt").write_text("hi")
    (src / "link").symlink_to("workspace/repo/f.txt")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    dest = tmp_path / "out"
    extract(archive, dest, max_expanded_bytes=10_000_000, max_members=_NO_MEMBER_CAP)
    assert (dest / "workspace" / "repo" / "f.txt").read_text() == "hi"
    assert (dest / "link").is_symlink()


def test_pack_extract_hardlinks_roundtrip(tmp_path) -> None:  # noqa: ANN001
    src = tmp_path / "home"
    src.mkdir()
    original = src / "original"
    original.write_bytes(b"payload")
    alias = src / "alias"
    alias.hardlink_to(original)
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    dest = tmp_path / "out"
    extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert (dest / "original").read_bytes() == (dest / "alias").read_bytes() == b"payload"
    assert (dest / "original").stat().st_ino == (dest / "alias").stat().st_ino


def test_pack_extract_parent_relative_symlink_roundtrip(tmp_path) -> None:  # noqa: ANN001
    src = tmp_path / "home"
    (src / "sub").mkdir(parents=True)
    (src / "original").write_bytes(b"payload")
    (src / "sub" / "link").symlink_to(Path("../original"))
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    dest = tmp_path / "out"
    extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert (dest / "sub" / "link").is_symlink()
    assert (dest / "sub" / "link").readlink() == Path("../original")
    assert (dest / "sub" / "link").read_bytes() == b"payload"


def test_pack_extract_rejects_own_pack_root_relative_link_escape(tmp_path) -> None:  # noqa: ANN001
    """I1 regression: our own `pack` emits root members as `./name`, whose parent becomes
    `["."]`  -- a no-op component that must not be treated as poppable by `..`, or a
    genuinely escaping root-level symlink slips past `_preflight` into extraction."""
    src = tmp_path / "home"
    src.mkdir()
    (src / "link").symlink_to(Path("../outside"))
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)

    # The escape must be caught by preflight itself -- the stdlib `data` filter's own late
    # rejection during `extractall` would otherwise mask a regressed preflight: dest/staging
    # end up absent either way, hiding that invalid links reached the extraction pass
    # instead of failing before any staging/write (the exact regression review I1 found).
    with pytest.raises(tarfile.TarError):
        _preflight(archive, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)

    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()
    assert list(tmp_path.glob(".out.staging-*")) == []


def test_extract_rejects_escaping_link_with_redundant_parent_components(tmp_path) -> None:  # noqa: ANN001
    """I1 regression: a redundant `.` parent component must not count as a poppable
    directory level -- `a/./link -> ../../outside` escapes past the archive root exactly
    as `a/link -> ../../outside` would, and must be rejected the same way."""
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("a/./link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        tar.addfile(info)

    with pytest.raises(tarfile.TarError):
        _preflight(archive, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)

    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()
    assert list(tmp_path.glob(".out.staging-*")) == []


def test_pack_over_cap_raises_and_removes(tmp_path) -> None:  # noqa: ANN001
    src = tmp_path / "home"
    src.mkdir()
    (src / "big").write_bytes(b"\0" * 10_000)
    archive = tmp_path / "a.tar.gz"
    with pytest.raises(ArchiveTooLarge):
        pack(src, archive, max_bytes=10)
    assert not archive.exists()


def test_pack_cleans_up_on_a_non_cap_error_too(tmp_path) -> None:  # noqa: ANN001
    """Review: "pack closes/unlinks its writer only for ArchiveTooLarge; another
    tar.add/tar close error exits without closing the writer or cleaning up the partial
    archive"."""
    archive = tmp_path / "a.tar.gz"
    with pytest.raises(OSError):
        pack(tmp_path / "does-not-exist", archive, max_bytes=10_000_000)
    assert not archive.exists()


def _single_member_archive(tmp_path, name: str, *, size: int = 1) -> Path:  # noqa: ANN001
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = size
        tar.addfile(info, io.BytesIO(b"x" * size))
    return archive


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a/../../escape"])
def test_extract_rejects_traversal(tmp_path, name: str) -> None:  # noqa: ANN001
    archive = _single_member_archive(tmp_path, name)
    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=100, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()  # preflight rejects before any destination write


def test_extract_rejects_bomb(tmp_path) -> None:  # noqa: ANN001
    src = tmp_path / "home"
    src.mkdir()
    (src / "zeros").write_bytes(b"\0" * 1_000_000)
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    with pytest.raises(ArchiveTooLarge):
        extract(archive, tmp_path / "out", max_expanded_bytes=1000, max_members=_NO_MEMBER_CAP)


def test_extract_rejects_too_many_members(tmp_path) -> None:  # noqa: ANN001
    archive = tmp_path / "many.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for i in range(5):
            info = tarfile.TarInfo(f"f{i}")
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(ArchiveTooLarge):
        extract(archive, tmp_path / "out", max_expanded_bytes=10_000, max_members=4)


def test_extract_rejects_escaping_symlink(tmp_path) -> None:  # noqa: ANN001
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("escape-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../outside"
        tar.addfile(info)
    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()


def test_extract_rejects_absolute_symlink_target(tmp_path) -> None:  # noqa: ANN001
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("abs-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()


@pytest.mark.parametrize("link_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
@pytest.mark.parametrize(
    "name,linkname",
    [
        ("escape-link", "/outside"),
        ("escape-link", "../outside"),
        ("sub/link", "../../outside"),
    ],
)
def test_extract_rejects_escaping_links_before_writes(  # noqa: ANN001
    tmp_path, link_type, name, linkname
) -> None:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.type = link_type
        info.linkname = linkname
        tar.addfile(info)
    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()
    assert list(tmp_path.glob(".out.staging-*")) == []


def test_extract_rejects_special_files(tmp_path) -> None:  # noqa: ANN001
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("dev-null")
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 3
        tar.addfile(info)
    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()


def test_extract_preflight_rejects_a_hostile_member_before_any_write(tmp_path) -> None:  # noqa: ANN001
    """A hostile member anywhere in the archive — even after valid ones — is caught by the
    full preflight pass, entirely before any destination directory is even created, let
    alone written into (review: "Validate all allowed member/path/link/type constraints
    before destination writes")."""
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        good = tarfile.TarInfo("ok.txt")
        good.size = 2
        tar.addfile(good, io.BytesIO(b"ok"))
        special = tarfile.TarInfo("dev-null")
        special.type = tarfile.CHRTYPE
        special.devmajor = 1
        special.devminor = 3
        tar.addfile(special)

    dest = tmp_path / "never-created"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()


def test_extract_refuses_pre_existing_destination_without_touching_it(tmp_path) -> None:  # noqa: ANN001
    """Critical fix: extraction must never write into (or delete) an already-existing
    destination — a prior valid Workspace/session copy must survive untouched even when the
    caller asks to extract into it."""
    src = tmp_path / "home"
    src.mkdir()
    (src / "new.txt").write_text("new")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)

    dest = tmp_path / "out"
    dest.mkdir()
    sentinel = dest / "sentinel.txt"
    sentinel.write_text("pre-existing valid state")

    with pytest.raises(FileExistsError):
        extract(archive, dest, max_expanded_bytes=10_000_000, max_members=_NO_MEMBER_CAP)

    assert sentinel.read_text() == "pre-existing valid state"
    assert not (dest / "new.txt").exists()  # nothing from the archive leaked in either


def test_extract_cleans_up_only_its_own_staging_on_late_failure(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """A failure during the real extraction pass (after preflight already passed) must clean
    up only the private staging directory — `dest` itself must never exist afterward, and no
    stray `.{name}.staging-*` directory is left behind either."""
    src = tmp_path / "home"
    src.mkdir()
    (src / "f.txt").write_text("hi")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)

    def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise OSError("simulated extraction failure")

    monkeypatch.setattr(tarfile.TarFile, "extractall", _boom)

    dest = tmp_path / "out"
    with pytest.raises(OSError, match="simulated extraction failure"):
        extract(archive, dest, max_expanded_bytes=10_000_000, max_members=_NO_MEMBER_CAP)

    assert not dest.exists()
    assert list(tmp_path.glob(".out.staging-*")) == []


@pytest.mark.parametrize("concurrent_dest", [False, True])
def test_extract_publication_failure_cleans_only_staging(  # noqa: ANN001
    tmp_path, monkeypatch, concurrent_dest
) -> None:
    """A failure during the final `rename` onto `dest` must clean up only the private
    staging directory; a destination that already exists (or was concurrently created)
    must survive untouched, and a genuinely absent destination must stay absent."""
    src = tmp_path / "home"
    src.mkdir()
    (src / "f.txt").write_text("hi")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)

    dest = tmp_path / "out"

    def _boom(self, target):  # noqa: ANN001, ANN202
        # Simulates another writer creating `dest` concurrently, in the window between
        # extract()'s upfront existence check and this rename -- the race the fix guards.
        if concurrent_dest:
            target.mkdir()
            (target / "sentinel").write_bytes(b"keep")
        raise OSError("simulated publication failure")

    monkeypatch.setattr(Path, "rename", _boom)

    with pytest.raises(OSError, match="simulated publication failure"):
        extract(archive, dest, max_expanded_bytes=10_000_000, max_members=_NO_MEMBER_CAP)

    assert list(tmp_path.glob(".out.staging-*")) == []
    if concurrent_dest:
        assert (dest / "sentinel").read_bytes() == b"keep"
    else:
        assert not dest.exists()


async def test_write_capped(tmp_path) -> None:  # noqa: ANN001
    async def chunks():  # noqa: ANN202
        for _ in range(3):
            yield b"abcd"

    dest = tmp_path / "x"
    assert await write_capped(chunks(), dest, max_bytes=12) == 12
    with pytest.raises(ArchiveTooLarge):
        await write_capped(chunks(), tmp_path / "y", max_bytes=11)
    assert not (tmp_path / "y").exists()


async def test_write_capped_cleans_up_on_cancellation(tmp_path) -> None:  # noqa: ANN001
    async def chunks():  # noqa: ANN202
        yield b"abcd"
        raise asyncio.CancelledError()

    dest = tmp_path / "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await write_capped(chunks(), dest, max_bytes=1_000)
    assert not dest.exists()


class _TrackingFile:
    """Wraps a real file handle, recording the thread each op ran on and optionally
    blocking on a `threading.Event` until released, to prove `write_capped`'s open/write/
    close run off the event loop and drain an in-flight op before unwinding cancellation."""

    def __init__(self, fh, *, hold_on: str | None = None, hold: threading.Event | None = None,
                 release: threading.Event | None = None, threads: list[int] | None = None,
                 closed: threading.Event | None = None) -> None:  # noqa: ANN001
        self._fh = fh
        self._hold_on = hold_on
        self._hold = hold
        self._release = release
        self._threads = threads
        self._closed = closed

    def _maybe_block(self, op: str) -> None:
        if self._threads is not None:
            self._threads.append(threading.get_ident())
        if op == self._hold_on and self._hold is not None and self._release is not None:
            self._hold.set()
            assert self._release.wait(timeout=5), f"watchdog: {op} was never released"

    def write(self, data: bytes) -> int:  # noqa: ANN001
        self._maybe_block("write")
        return self._fh.write(data)

    def close(self) -> None:
        self._maybe_block("close")
        self._fh.close()
        if self._closed is not None:
            self._closed.set()


async def test_write_capped_file_lifecycle_is_off_loop(tmp_path) -> None:  # noqa: ANN001
    loop_thread_id = threading.get_ident()
    file_operation_threads: list[int] = []
    hold = threading.Event()
    release = threading.Event()
    heartbeat_seen = False

    real_open = Path.open

    def _tracking_open(self, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        file_operation_threads.append(threading.get_ident())
        fh = real_open(self, mode, *args, **kwargs)
        return _TrackingFile(
            fh, hold_on="write", hold=hold, release=release, threads=file_operation_threads
        )

    async def chunks():  # noqa: ANN202
        for _ in range(3):
            yield b"abcd"

    async def wait_for_hold_then_release() -> None:
        nonlocal heartbeat_seen
        while not hold.is_set():
            await asyncio.sleep(0)
        heartbeat_seen = True
        release.set()

    dest = tmp_path / "x"
    with mock.patch.object(Path, "open", _tracking_open):
        try:
            total, _ = await asyncio.wait_for(
                asyncio.gather(
                    write_capped(chunks(), dest, max_bytes=12), wait_for_hold_then_release()
                ),
                timeout=5,
            )
        finally:
            release.set()

    assert total == 12
    assert dest.read_bytes() == b"abcd" * 3
    assert all(thread_id != loop_thread_id for thread_id in file_operation_threads)
    assert heartbeat_seen


@pytest.mark.parametrize("held_op", ["open", "write", "close"])
async def test_write_capped_cancellation_drains_file_worker(tmp_path, held_op) -> None:  # noqa: ANN001
    dest = tmp_path / "cancelled-drain"
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")

    hold = threading.Event()
    release = threading.Event()
    handle_closed = threading.Event()
    coroutine_exited = threading.Event()

    real_open = Path.open

    def _tracking_open(self, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if held_op == "open":
            hold.set()
            assert release.wait(timeout=5), "watchdog: open was never released"
        fh = real_open(self, mode, *args, **kwargs)
        return _TrackingFile(
            fh,
            hold_on=held_op if held_op != "open" else None,
            hold=hold,
            release=release,
            closed=handle_closed,
        )

    async def chunks():  # noqa: ANN202
        yield b"abcd"

    async def run() -> None:
        try:
            await write_capped(chunks(), dest, max_bytes=1_000)
        finally:
            # Set once `write_capped` returns, which is only after `_run_owned` has drained
            # the real worker thread to completion -- this marks coroutine exit, not the
            # worker thread itself; `handle_closed` (set inside the real `close()`, which
            # runs on that thread) is the actual thread-completion signal.
            coroutine_exited.set()

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    with mock.patch.object(Path, "open", _tracking_open):
        task = asyncio.ensure_future(run())
        try:
            await asyncio.wait_for(wait_for_hold(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # a second cancellation while still held must also drain, not corrupt state
            await asyncio.sleep(0)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    assert handle_closed.is_set()
    assert coroutine_exited.is_set()
    assert not dest.exists()
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("held_op", ["open", "write", "close"])
async def test_write_capped_cancellation_before_failure_stays_cancelled(  # noqa: ANN001
    tmp_path, held_op
) -> None:
    """I2 regression: a cancellation observed before a held worker's own failure must still
    surface as `CancelledError` -- not the worker's exception -- once draining, closing the
    owned handle and unlinking the partial file finish. An ordinary first I/O error without
    a preceding cancellation is already covered by
    `test_write_capped_io_failure_cleans_owned_file`."""
    dest = tmp_path / "cancelled-then-failed"
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")

    hold = threading.Event()
    release = threading.Event()
    handle_closed = threading.Event()
    coroutine_exited = threading.Event()

    real_open = Path.open

    class _HoldThenFailFile:
        def __init__(self, fh) -> None:  # noqa: ANN001
            self._fh = fh

        def _await_release(self, op: str) -> None:
            hold.set()
            assert release.wait(timeout=5), f"watchdog: {op} was never released"

        def write(self, data: bytes) -> int:  # noqa: ANN001
            if held_op == "write":
                self._await_release("write")
                raise OSError("simulated write failure after cancellation")
            return self._fh.write(data)

        def close(self) -> None:
            if held_op == "close":
                self._await_release("close")
                self._fh.close()  # close for real before raising -- same fix as the sibling
                handle_closed.set()
                raise OSError("simulated close failure after cancellation")
            self._fh.close()
            handle_closed.set()

    def _tracking_open(self, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if held_op == "open":
            hold.set()
            assert release.wait(timeout=5), "watchdog: open was never released"
            raise OSError("simulated open failure after cancellation")
        return _HoldThenFailFile(real_open(self, mode, *args, **kwargs))

    async def chunks():  # noqa: ANN202
        yield b"abcd"

    async def run() -> None:
        try:
            await write_capped(chunks(), dest, max_bytes=1_000)
        finally:
            coroutine_exited.set()

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    with mock.patch.object(Path, "open", _tracking_open):
        task = asyncio.ensure_future(run())
        try:
            await asyncio.wait_for(wait_for_hold(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # repeated cancellation while still held must also drain cleanly
            await asyncio.sleep(0)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    assert coroutine_exited.is_set()
    if held_op != "open":
        assert handle_closed.is_set()
    assert not dest.exists()
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("held_op", ["close"])
async def test_write_capped_write_error_survives_close_cancellation(  # noqa: ANN001
    tmp_path, held_op
) -> None:
    """I2-R1 reverse-ordering regression: an ordinary first write error recorded before
    cleanup begins must survive a cancellation observed only during the subsequent close.
    Preserve the already received primary outcome when entering cleanup; later cleanup
    cancellation must not replace it with `CancelledError`."""
    dest = tmp_path / "write-error-then-cancelled-close"
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")

    hold = threading.Event()
    release = threading.Event()
    handle_closed = threading.Event()
    coroutine_exited = threading.Event()

    real_open = Path.open

    class _FailWriteThenHoldCloseFile:
        def __init__(self, fh) -> None:  # noqa: ANN001
            self._fh = fh

        def write(self, data: bytes) -> int:  # noqa: ANN001
            raise OSError("simulated write failure before close cancellation")

        def close(self) -> None:
            hold.set()
            assert release.wait(timeout=5), "watchdog: close was never released"
            self._fh.close()
            handle_closed.set()

    def _tracking_open(self, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _FailWriteThenHoldCloseFile(real_open(self, mode, *args, **kwargs))

    async def chunks():  # noqa: ANN202
        yield b"abcd"

    async def run() -> BaseException | None:
        try:
            await write_capped(chunks(), dest, max_bytes=1_000)
        except BaseException as caught:  # noqa: BLE001 - captured for the assertion below
            return caught
        finally:
            coroutine_exited.set()
        return None

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    with mock.patch.object(Path, "open", _tracking_open):
        task = asyncio.ensure_future(run())
        try:
            await asyncio.wait_for(wait_for_hold(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # repeated cancellation during close must not replace the write error
            await asyncio.sleep(0)
        finally:
            release.set()
            result = await asyncio.wait_for(task, timeout=5)

    assert coroutine_exited.is_set()
    assert isinstance(result, OSError)
    assert str(result) == "simulated write failure before close cancellation"
    assert handle_closed.is_set()
    assert not dest.exists()
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("failing_op", ["open", "write", "close"])
async def test_write_capped_io_failure_cleans_owned_file(tmp_path, failing_op) -> None:  # noqa: ANN001
    dest = tmp_path / "boom"
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")

    real_open = Path.open
    opened_handles: list = []

    class _FailingFile:
        def __init__(self, fh) -> None:  # noqa: ANN001
            self._fh = fh
            opened_handles.append(fh)

        def write(self, data: bytes) -> int:  # noqa: ANN001
            if failing_op == "write":
                raise OSError("simulated write failure")
            return self._fh.write(data)

        def close(self) -> None:
            if failing_op == "close":
                self._fh.close()  # close for real -- a failure must not overclaim the
                # handle stayed open when the underlying close itself actually succeeded
                raise OSError("simulated close failure")
            self._fh.close()

    def _failing_open(self, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if failing_op == "open":
            raise OSError("simulated open failure")
        return _FailingFile(real_open(self, mode, *args, **kwargs))

    async def chunks():  # noqa: ANN202
        yield b"abcd"

    with mock.patch.object(Path, "open", _failing_open), pytest.raises(OSError):
        await write_capped(chunks(), dest, max_bytes=12)

    assert not dest.exists()
    assert unrelated.read_bytes() == b"keep"
    if failing_op != "open":
        assert opened_handles[0].closed


def test_no_unsafe_filter_parameter_is_exposed(tmp_path) -> None:  # noqa: ANN001
    """Review + dispatch both require: no unsafe extraction mode for agent archives. An
    absolute symlink target must not survive extraction — only PEP 706's `data` filter ever
    runs, with no caller-selectable escape hatch."""
    src = tmp_path / "home"
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "python").symlink_to("/usr/bin/python3")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    dest = tmp_path / "out"
    with pytest.raises(tarfile.TarError):
        extract(archive, dest, max_expanded_bytes=10_000_000, max_members=_NO_MEMBER_CAP)
    assert not dest.exists()

    assert "filter" not in inspect.signature(extract).parameters

# SPDX-License-Identifier: Apache-2.0
"""Task 0A characterization of private preparation and workspace handoff.

These tests deliberately exercise the current script hook, rather than a proposed
private-checkout implementation.  The security tests are strict xfails until Task
0B removes the contaminated Git configuration path.
"""

from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ach_agent.boot.prepare import PrepareFailed, prepare_workspace, run_cleanup, run_prepare
from ach_agent.boot.private_prepare import (
    PrivateBundle,
    PrivateCleanupRegistry,
    PrivatePrepareFailed,
    _git_env,
    _produce_bundle,
    _public_origin,
    _publish_bundle,
    _scan_git_objects,
    dispose_private_bundle,
    private_prepare,
)
from ach_agent.boot.private_prepare import (
    _git as private_git,
)
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock
from ach_agent.execution.wire import WorkspaceStoppedEvent


def _event(number: int = 1) -> MessageEvent:
    return MessageEvent(
        idempotency_key=f"evt-{number}",
        session_key="group/project:7",
        channel_name="gitlab-mr-review",
        delivery_context={"project_path": "group/project", "head_sha": "unused"},
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def _local_origin(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "fixture")
    (source / "notes.txt").write_text("origin\n")
    _git(source, "add", "notes.txt")
    _git(source, "commit", "-qm", "initial")
    return source, _git(source, "rev-parse", "HEAD")


def _clone_block(source: Path) -> PrepareBlock:
    return PrepareBlock.model_validate(
        {
            "script": """
set -eu
REPO="$ACH_WORKSPACE/repo"
if [ -d "$REPO/.git" ]; then
  git -C "$REPO" fetch -q origin
else
  git clone -q "$SOURCE" "$REPO"
fi
git -C "$REPO" checkout -q --force --detach origin/main
""",
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )


def test_private_git_environment_disables_promisor_lazy_fetch() -> None:
    assert _git_env()["GIT_NO_LAZY_FETCH"] == "1"


async def test_private_prepare_explains_incomplete_promisor_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, initial_head = _local_origin(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private_root = tmp_path / "private"
    private_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    async def fake_git(
        repo: Path,
        *args: str,
        env: dict[str, str],
        timeout: int = 120,
        secret_values: tuple[str, ...] = (),
    ) -> str:
        del repo, env, timeout, secret_values
        if args == ("rev-parse", "HEAD"):
            return initial_head
        if args == ("remote", "get-url", "origin"):
            return str(source)
        if args[:2] == ("bundle", "create"):
            raise PrivatePrepareFailed(
                "private Git handoff failed: fatal: could not fetch missing blob from "
                "promisor remote"
            )
        raise AssertionError(args)

    monkeypatch.setattr("ach_agent.boot.private_prepare._git", fake_git)
    monkeypatch.setattr("ach_agent.boot.private_prepare._scan_git_objects", _noop_scan)

    with pytest.raises(
        PrivatePrepareFailed,
        match=(
            "fully materialized Git checkout.*fetch missing objects while credentials are available"
        ),
    ):
        await _produce_bundle(source, workspace, private_root, home, ())


async def _noop_scan(
    source: Path,
    env: dict[str, str],
    secret_values: tuple[str, ...],
    timeout: int = 120,
) -> None:
    del source, env, secret_values, timeout


def _authenticated_git_http_server(
    root: Path,
) -> tuple[ThreadingHTTPServer, threading.Thread, dict[str, int]]:
    counts = {"authorized": 0, "unauthorized": 0}
    expected = "Basic " + base64.b64encode(b"oauth2:synthetic-filter-token").decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            self._serve_git()

        def do_POST(self) -> None:
            self._serve_git()

        def _serve_git(self) -> None:
            if self.headers.get("Authorization") != expected:
                counts["unauthorized"] += 1
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="synthetic"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            counts["authorized"] += 1
            target = urlsplit(self.path)
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            env = {
                **os.environ,
                "GIT_PROJECT_ROOT": str(root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": target.path,
                "QUERY_STRING": target.query,
                "REQUEST_METHOD": self.command,
                "REMOTE_USER": "synthetic",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
            }
            if protocol := self.headers.get("Git-Protocol"):
                env["HTTP_GIT_PROTOCOL"] = protocol
            result = subprocess.run(
                ["git", "http-backend"], env=env, input=body, capture_output=True, check=True
            )
            headers, data = result.stdout.split(b"\r\n\r\n", 1)
            self.send_response(200)
            for line in headers.decode().split("\r\n"):
                key, value = line.split(":", 1)
                self.send_header(key, value.strip())
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, counts


def _http_origin(tmp_path: Path) -> tuple[Path, str]:
    source, _ = _local_origin(tmp_path)
    (source / "notes.txt").write_text("historical-1\n")
    _git(source, "add", "notes.txt")
    _git(source, "commit", "-qm", "historical")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(source), str(origin)], check=True)
    subprocess.run(
        ["git", "--git-dir", str(origin), "config", "uploadpack.allowFilter", "true"],
        check=True,
    )
    return origin, _git(source, "rev-parse", "HEAD")


def _http_prepare_block(url: str, *, filtered: bool) -> PrepareBlock:
    option = "--filter=blob:none " if filtered else ""
    return PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; AUTH=$(printf "oauth2:%s" "$TOKEN" | base64 -w0); '
                "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader; "
                'export GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH"; '
                f'git clone {option}--no-recurse-submodules "$SOURCE" "$ACH_WORKSPACE/repo"'
            ),
            "env": {"SOURCE": url},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )


@pytest.mark.parametrize("filtered", [False, True])
async def test_authenticated_http_prepare_materialization_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filtered: bool
) -> None:
    origin, head = _http_origin(tmp_path)
    server, thread, counts = _authenticated_git_http_server(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-filter-token")
    workspace = tmp_path / ("filtered-workspace" if filtered else "complete-workspace")
    workspace.mkdir()
    try:
        cfg = _http_prepare_block(
            f"http://127.0.0.1:{server.server_port}/{origin.name}", filtered=filtered
        )
        if filtered:
            with pytest.raises(
                PrivatePrepareFailed,
                match=(
                    "fully materialized Git checkout.*fetch missing objects while credentials "
                    "are available"
                ),
            ):
                await private_prepare(cfg, _event(), workspace, tmp_path / "scratch")
            assert counts["authorized"] > 0
            assert counts["unauthorized"] == 0
        else:
            bundle = await private_prepare(cfg, _event(), workspace, tmp_path / "scratch")
            assert bundle.head == head
            assert counts["authorized"] > 0
            assert counts["unauthorized"] == 0
            assert b"synthetic-filter-token" not in (workspace / bundle.path).read_bytes()
            dispose_private_bundle(bundle, workspace)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_private_fixture_characterizes_reuse_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, initial_head = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    home = tmp_path / "home"
    workspace = prepare_workspace(str(home), str(tmp_path / "work"), "group/project:7")
    inode = workspace.stat().st_ino
    cfg = _clone_block(source)

    await run_prepare(cfg, _event(), workspace)
    repo = workspace / "repo"
    assert _git(repo, "rev-parse", "origin/main") == initial_head
    assert _git(repo, "merge-base", "origin/main", "HEAD") == initial_head
    assert _git(repo, "remote", "get-url", "origin") == str(source)
    _git(repo, "config", "user.email", "agent@example.invalid")
    _git(repo, "config", "user.name", "agent")
    assert workspace.stat().st_ino == inode
    assert (workspace / ".ach-state").resolve() == (home / ".ach-state").resolve()
    assert _git(repo, "rev-parse", "HEAD") == initial_head
    assert (repo / "notes.txt").read_text() == "origin\n"

    # A populated checkout is reused. The current reference script's force checkout
    # discards tracked edits, while unrelated files and local commits remain observable.
    (repo / "notes.txt").write_text("dirty agent edit\n")
    await run_prepare(cfg, _event(2), workspace)
    assert (repo / "notes.txt").read_text() == "origin\n"
    assert workspace.stat().st_ino == inode

    (repo / "notes.txt").write_text("dirty agent edit\n")
    (repo / "untracked.txt").write_text("retain me\n")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-qm", "agent local commit")
    local_head = _git(repo, "rev-parse", "HEAD")
    await run_prepare(cfg, _event(3), workspace)
    assert workspace.stat().st_ino == inode
    assert _git(repo, "rev-parse", "HEAD") == initial_head
    assert (repo / "notes.txt").read_text() == "origin\n"
    assert (repo / "untracked.txt").read_text() == "retain me\n"
    assert _git(repo, "cat-file", "-t", local_head) == "commit"

    await run_cleanup(
        PrepareBlock.model_validate({"script": 'rm -rf -- "$ACH_WORKSPACE/repo"'}),
        _event(2),
        workspace,
    )
    assert workspace.stat().st_ino == inode
    assert not repo.exists()
    assert (workspace / ".ach-state").is_symlink()


async def test_private_prepare_produces_shared_bundle_without_destination_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private preparation publishes metadata and a bundle, leaving destination import to H2."""
    source, initial_head = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "producer")
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"; '
                'git -C "$ACH_WORKSPACE/repo" checkout -q --detach main'
            ),
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )

    bundle = await private_prepare(cfg, _event(), workspace, tmp_path / "scratch")

    assert bundle.head == initial_head
    assert bundle.origin == str(source)  # configured shared local origins remain compatible
    assert not Path(bundle.path).is_absolute()
    assert (workspace / bundle.path).is_file()
    assert not (workspace / "repo").exists()


async def test_private_prepare_scrubs_private_scratch_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "private-origin")
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"; '
                'git -C "$ACH_WORKSPACE/repo" remote set-url origin "$ACH_WORKSPACE/repo"'
            ),
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )

    bundle = await private_prepare(cfg, _event(), workspace, tmp_path / "scratch")

    assert bundle.origin is None
    assert str(tmp_path / "scratch") not in bundle.path


async def test_private_cleanup_registry_correlates_and_acknowledges_event(
    tmp_path: Path,
) -> None:
    event = _event()
    cfg = PrepareBlock.model_validate({"script": "true"})
    registry = PrivateCleanupRegistry(max_contexts=2)
    await registry.register(
        "invocation",
        event,
        tmp_path / "workspace",
        tmp_path / "scratch",
        cfg,
    )
    mismatched = WorkspaceStoppedEvent(
        controller_id="controller",
        instance_id="instance",
        session_key=event.session_key,
        event_id="wrong",
        invocation_id="invocation",
        workspace=str(tmp_path / "workspace"),
    )
    acknowledgements: list[str] = []
    assert not await registry.handle_event(mismatched, lambda _: _record_ack(acknowledgements))

    stopped = WorkspaceStoppedEvent(
        controller_id="controller",
        instance_id="instance",
        session_key=event.session_key,
        event_id=event.idempotency_key,
        invocation_id="invocation",
        workspace=str(tmp_path / "workspace"),
    )
    assert await registry.handle_event(stopped, lambda value: _record_ack(acknowledgements, value))
    for _ in range(100):
        if acknowledgements:
            break
        await asyncio.sleep(0.01)
    assert acknowledgements == ["invocation"]
    await registry.close()


async def test_private_cleanup_registry_dispatches_all_bounded_callbacks(
    tmp_path: Path,
) -> None:
    cfg = PrepareBlock.model_validate({"script": "true"})
    acknowledgements: list[str] = []
    registry = PrivateCleanupRegistry(max_contexts=64)
    stopped: list[WorkspaceStoppedEvent] = []
    for number in range(10):
        event = _event(number + 1)
        invocation_id = f"invocation-{number}"
        await registry.register(
            invocation_id, event, tmp_path / "workspace", tmp_path / "scratch", cfg
        )
        stopped.append(
            WorkspaceStoppedEvent(
                controller_id="controller",
                instance_id="instance",
                session_key=event.session_key,
                event_id=event.idempotency_key,
                invocation_id=invocation_id,
                workspace=str(tmp_path / "workspace"),
            )
        )
    for item in stopped:
        assert await registry.handle_event(item, lambda value: _record_ack(acknowledgements, value))
    for _ in range(100):
        if len(acknowledgements) == len(stopped):
            break
        await asyncio.sleep(0.01)
    assert sorted(acknowledgements) == sorted(item.invocation_id for item in stopped)
    await registry.close()


async def _record_ack(
    acknowledgements: list[str], event: WorkspaceStoppedEvent | None = None
) -> None:
    if event is not None:
        acknowledgements.append(event.invocation_id)


def test_bundle_publish_rejects_existing_artifact_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_bundle = tmp_path / "private.bundle"
    private_bundle.write_bytes(b"bundle")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.bundle"
    outside.write_bytes(b"keep")
    monkeypatch.setattr("ach_agent.boot.private_prepare.secrets.token_hex", lambda _length: "fixed")
    (workspace / ".ach-private-fixed.bundle").symlink_to(outside)

    with pytest.raises(PrivatePrepareFailed, match="could not be published"):
        _publish_bundle(private_bundle, workspace)

    assert (workspace / ".ach-private-fixed.bundle").is_symlink()
    assert outside.read_bytes() == b"keep"


def test_dispose_private_bundle_uses_owned_relative_artifact_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / ".ach-private-owned.bundle"
    artifact.write_bytes(b"bundle")
    dispose_private_bundle(PrivateBundle(artifact.name, "a" * 40, None), workspace)
    assert not artifact.exists()

    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    with pytest.raises(PrivatePrepareFailed, match="not relative"):
        dispose_private_bundle(PrivateBundle(str(outside), "a" * 40, None), workspace)
    assert outside.read_bytes() == b"keep"


def test_private_origin_scrubs_file_uri_into_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    private_repo = scratch / "prepare-1" / "work" / "repo"
    private_repo.mkdir(parents=True)
    assert _public_origin(f"file://{private_repo}", scratch) is None
    assert _public_origin("file:///configured/shared/repo", scratch) == (
        "file:///configured/shared/repo"
    )


def test_bundle_publish_rejects_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    private_bundle = tmp_path / "private.bundle"
    private_bundle.write_bytes(b"bundle")
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    workspace = real_parent / "workspace"
    workspace.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(PrivatePrepareFailed, match="could not be opened safely"):
        _publish_bundle(private_bundle, linked_parent / "workspace")


async def test_prepare_failure_is_fail_closed_and_retains_workspace(tmp_path: Path) -> None:
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "failure")
    marker = workspace / "started"
    cfg = PrepareBlock.model_validate({"script": f"touch {marker}; exit 19"})
    with pytest.raises(PrepareFailed, match="exited 19"):
        await run_prepare(cfg, _event(), workspace)
    assert marker.exists()
    assert workspace.exists()


async def test_private_git_helper_cancellation_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    parent_pid = tmp_path / "parent.pid"
    child_pid = tmp_path / "child.pid"
    fake_git = bindir / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {parent_pid}\n"
        f"(sleep 30) & child=$!; echo $child > {child_pid}; wait $child\n"
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    task = asyncio.create_task(private_git(tmp_path, "status", env=_git_env()))
    async with asyncio.timeout(2):
        while not parent_pid.exists() or not child_pid.exists():
            await asyncio.sleep(0.01)
    parent = int(parent_pid.read_text())
    child = int(child_pid.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for pid in (parent, child):
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            stat_path = Path(f"/proc/{pid}/stat")
            if stat_path.exists() and stat_path.read_text().split()[2] == "Z":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(f"process {pid} survived cancellation")


async def test_private_git_error_is_redacted_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    token = "synthetic-private-token"
    fake_git = bindir / "git"
    fake_git.write_text(
        f'#!/bin/sh\ni=0; while [ "$i" -lt 20000 ]; do printf x >&2; i=$((i + 1)); done; '
        f'printf "%s" "{token}" >&2; exit 19\n'
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    with pytest.raises(PrivatePrepareFailed) as error:
        await private_git(tmp_path, "status", env=_git_env(), secret_values=(token,))
    message = str(error.value)
    assert token not in message
    assert "[REDACTED]" in message
    assert len(message) < 4300


async def test_private_object_scan_rejects_malformed_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_git = bindir / "git"
    fake_git.write_text("#!/bin/sh\nprintf 'malformed-header\\n'\n")
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    with pytest.raises(PrivatePrepareFailed, match="invalid metadata"):
        await _scan_git_objects(tmp_path, _git_env(), ("token",))


async def test_private_object_scan_timeout_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pid_file = tmp_path / "scanner.pid"
    fake_git = bindir / "git"
    fake_git.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nsleep 30\n")
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    with pytest.raises(TimeoutError):
        await _scan_git_objects(tmp_path, _git_env(), ("token",), timeout=1)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

# SPDX-License-Identifier: Apache-2.0
"""Small, role-owned bootstrap files for the split deployment.

The harness is the only writer.  Channels receive their source projection and
the channels/harness authentication key; the engine receives only the existing
``PublicEngineConfig`` wire object.  This module intentionally has no watcher or
reload path: a changed bootstrap is consumed on the next process start.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue

from ach_agent.execution.wire import PublicEngineConfig

DEFAULT_CHANNELS_BOOTSTRAP_PATH = Path("/run/ach-agent/channels/bootstrap.json")
DEFAULT_ENGINE_BOOTSTRAP_PATH = Path("/run/ach-agent/engine/bootstrap.json")
DEFAULT_BOOTSTRAP_WAIT_SECONDS = 300.0
DEFAULT_HARNESS_URL = "http://127.0.0.1:8090"
DEFAULT_ENGINE_URL = "http://127.0.0.1:8081"
DEFAULT_CHANNELS_HOST = "0.0.0.0"
DEFAULT_CHANNELS_PORT = 8080
DEFAULT_HARNESS_HOST = "127.0.0.1"
DEFAULT_HARNESS_PORT = 8090
DEFAULT_ENGINE_HOST = "127.0.0.1"
DEFAULT_ENGINE_PORT = 8081
MAX_BOOTSTRAP_BYTES = 1024 * 1024


class BootstrapError(RuntimeError):
    """A bootstrap is unavailable, malformed, or unsafe to consume."""


class BootstrapUnavailable(BootstrapError):
    """The bootstrap has not been published yet."""


@dataclass(frozen=True, slots=True)
class BootstrapPaths:
    """The two narrow files shared by the split role containers."""

    channels: Path = DEFAULT_CHANNELS_BOOTSTRAP_PATH
    engine: Path = DEFAULT_ENGINE_BOOTSTRAP_PATH


@dataclass(frozen=True, slots=True)
class ChannelsBootstrap:
    """Validated channels-side projection and its stable shared key."""

    agent_name: str
    harness_url: str
    hmac_key: str
    channels: list[dict[str, JsonValue]]


def role_bootstrap_path(role: str) -> Path:
    """Return a role's bootstrap path, preserving deliberate path overrides."""
    if role == "channels":
        name = "ACH_CHANNELS_BOOTSTRAP_PATH"
        default = DEFAULT_CHANNELS_BOOTSTRAP_PATH
    elif role == "engine":
        name = "ACH_ENGINE_BOOTSTRAP_PATH"
        default = DEFAULT_ENGINE_BOOTSTRAP_PATH
    else:  # pragma: no cover - callers only pass argparse-constrained roles
        raise ValueError(f"unknown bootstrap role: {role}")
    value = os.environ.get(name, "").strip()
    return Path(value) if value else default


def bootstrap_paths() -> BootstrapPaths:
    """Resolve both bootstrap files using fixed defaults and explicit overrides."""
    return BootstrapPaths(
        channels=role_bootstrap_path("channels"),
        engine=role_bootstrap_path("engine"),
    )


def _read_json(path: Path, *, max_bytes: int = MAX_BOOTSTRAP_BYTES) -> dict[str, Any]:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise BootstrapUnavailable("bootstrap did not become available") from exc
    except OSError as exc:
        raise BootstrapError("bootstrap file cannot be opened") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BootstrapError("bootstrap must be a regular file")
        if info.st_size > max_bytes:
            raise BootstrapError("bootstrap exceeds the size limit")
        payload = b""
        while len(payload) <= max_bytes:
            chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) > max_bytes:
            raise BootstrapError("bootstrap exceeds the size limit")
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError("bootstrap file cannot be read") from exc
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("bootstrap contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise BootstrapError("bootstrap must contain a JSON object")
    return cast(dict[str, Any], value)


def _invalid(kind: str) -> BootstrapError:
    # Keep the exception deliberately value-free.  Channels bundles contain the
    # HMAC key, and Pydantic's default validation errors echo input values.
    return BootstrapError(f"invalid {kind} bootstrap")


def read_channels_bootstrap(
    path: str | Path = DEFAULT_CHANNELS_BOOTSTRAP_PATH,
    *,
    max_bytes: int = MAX_BOOTSTRAP_BYTES,
) -> ChannelsBootstrap:
    """Read and validate a channels bundle without exposing its secret in errors."""
    try:
        raw = _read_json(Path(path), max_bytes=max_bytes)
        if raw.get("schemaVersion") != "1":
            raise _invalid("channels")
        agent_name = raw.get("agentName")
        harness_url = raw.get("harnessUrl")
        hmac_key = raw.get("hmacKey")
        sources = raw.get("channels")
        if (
            not isinstance(agent_name, str)
            or not agent_name.strip()
            or not isinstance(harness_url, str)
            or not harness_url.strip()
            or not isinstance(hmac_key, str)
            or not hmac_key
            or not isinstance(sources, list)
            or any(not isinstance(source, dict) for source in sources)
        ):
            raise _invalid("channels")
        return ChannelsBootstrap(
            agent_name=agent_name,
            harness_url=harness_url,
            hmac_key=hmac_key,
            channels=cast(list[dict[str, JsonValue]], sources),
        )
    except BootstrapUnavailable:
        raise
    except BootstrapError:
        raise _invalid("channels") from None
    except Exception:
        raise _invalid("channels") from None


def read_engine_bootstrap(
    path: str | Path = DEFAULT_ENGINE_BOOTSTRAP_PATH,
    *,
    max_bytes: int = MAX_BOOTSTRAP_BYTES,
) -> dict[str, JsonValue]:
    """Read only the credential-free ``PublicEngineConfig`` object."""
    try:
        raw = _read_json(Path(path), max_bytes=max_bytes)
        config = PublicEngineConfig.model_validate(raw)
        return cast(dict[str, JsonValue], config.model_dump(mode="json", by_alias=True))
    except BootstrapUnavailable:
        raise
    except BootstrapError:
        raise _invalid("engine") from None
    except Exception:
        raise _invalid("engine") from None


def _existing_channels(path: Path) -> ChannelsBootstrap | None:
    if not path.exists() and not path.is_symlink():
        return None
    return read_channels_bootstrap(path)


def _existing_engine(path: Path) -> None:
    if path.exists() or path.is_symlink():
        read_engine_bootstrap(path)


def _atomic_write(path: Path, value: dict[str, JsonValue]) -> None:
    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) + 1 > MAX_BOOTSTRAP_BYTES:
            raise BootstrapError("bootstrap exceeds the size limit")
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.write(b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            path.chmod(0o600)
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BootstrapError:
            raise
        except OSError as exc:
            raise BootstrapError("bootstrap publication failed") from exc
        finally:
            temporary_path.unlink(missing_ok=True)
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError("bootstrap publication failed") from exc


@dataclass(frozen=True, slots=True)
class PublishedBootstraps:
    channels_path: Path
    engine_path: Path
    hmac_key: str


def publish_bootstraps(
    paths: BootstrapPaths,
    channels: dict[str, JsonValue],
    public_engine: dict[str, JsonValue],
    *,
    agent_name: str,
    harness_url: str = DEFAULT_HARNESS_URL,
    hmac_key: str | None = None,
) -> PublishedBootstraps:
    """Publish both projections, retaining the existing key across H restarts."""
    existing_channels = _existing_channels(paths.channels)
    _existing_engine(paths.engine)
    if not agent_name.strip() or not harness_url.strip():
        raise BootstrapError("bootstrap identity and URL are required")
    if hmac_key is not None and not hmac_key:
        raise BootstrapError("bootstrap key is invalid")
    if (
        hmac_key is not None
        and existing_channels is not None
        and hmac_key != existing_channels.hmac_key
    ):
        raise BootstrapError("bootstrap key does not match existing channels bundle")
    chosen_key = hmac_key or (existing_channels.hmac_key if existing_channels else "")
    if not chosen_key:
        chosen_key = secrets.token_urlsafe(32)
    if not isinstance(channels.get("channels"), list) or any(
        not isinstance(source, dict) for source in cast(list[Any], channels["channels"])
    ):
        raise BootstrapError("invalid channels bootstrap projection")
    try:
        PublicEngineConfig.model_validate(public_engine)
    except Exception:
        raise _invalid("engine") from None
    bundle: dict[str, JsonValue] = {
        "schemaVersion": "1",
        "agentName": agent_name,
        "harnessUrl": harness_url,
        "hmacKey": chosen_key,
        "channels": channels["channels"],
    }
    _atomic_write(paths.channels, bundle)
    _atomic_write(paths.engine, public_engine)
    return PublishedBootstraps(paths.channels, paths.engine, chosen_key)


async def wait_for_channels_bootstrap(
    path: str | Path = DEFAULT_CHANNELS_BOOTSTRAP_PATH,
    *,
    timeout: float = DEFAULT_BOOTSTRAP_WAIT_SECONDS,
    poll_interval: float = 0.2,
) -> ChannelsBootstrap:
    """Wait for H publication, then fail closed on malformed content."""
    return await _wait_for(lambda: read_channels_bootstrap(path), timeout, poll_interval)


async def wait_for_engine_bootstrap(
    path: str | Path = DEFAULT_ENGINE_BOOTSTRAP_PATH,
    *,
    timeout: float = DEFAULT_BOOTSTRAP_WAIT_SECONDS,
    poll_interval: float = 0.2,
) -> dict[str, JsonValue]:
    """Wait for H publication, then fail closed on malformed content."""
    return await _wait_for(lambda: read_engine_bootstrap(path), timeout, poll_interval)


async def _wait_for[T](reader: Callable[[], T], timeout: float, poll_interval: float) -> T:
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("bootstrap timeout and poll interval must be positive")
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return reader()
        except BootstrapUnavailable:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BootstrapError("bootstrap did not become available before timeout")
            await asyncio.sleep(min(poll_interval, remaining))

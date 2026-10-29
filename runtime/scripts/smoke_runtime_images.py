#!/usr/bin/env python3
"""Static checks and bounded runtime smoke for the packaged runtime images."""

from __future__ import annotations

import argparse
import json
import select
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTROL_IMAGE = "ach-runtime-control:0.1.0"
EXECUTION_IMAGE = "ach-runtime-execution:0.1.0"
BOOT_FIXTURE = ROOT / "tests/config/fixtures/bootstrap.json"
PUBLIC_FIXTURE = ROOT / "tests/config/fixtures/pr-review-runtime.json"
FACADE_FIXTURE = ROOT / "spikes/opencode-v2/fake-facades.mjs"
IMAGE_MEMORY = "512m"


def check() -> list[str]:
    errors: list[str] = []

    def read(name: str) -> str:
        path = ROOT / name
        if not path.is_file():
            errors.append(f"missing {name}")
            return ""
        return path.read_text()

    dockerfile = read("Dockerfile")
    ignore = read(".dockerignore")
    compose = read("docker-compose.yml")
    driver = read("scripts/smoke_runtime_images.py")

    for target in ("builder", "opencode-builder", "runtime", "control", "execution"):
        if f"AS {target}" not in dockerfile:
            errors.append(f"Dockerfile is missing stage {target}")
    for value in (
        "python:3.13.15-alpine3.24",
        "node:22.23.3-alpine3.24",
        "@opencode/cli@2.0.20",
        "COPY pyproject.toml",
        "COPY src/",
        "COPY plugins/",
        'ENTRYPOINT ["python", "-m", "ach_runtime"]',
        'CMD ["control", "--config", "/etc/ach-runtime/config.json"]',
        'CMD ["execution", "--config", "/etc/ach-runtime/bootstrap.json"]',
        "OPENCODE_BIN=/opt/opencode/node_modules/.bin/opencode2",
    ):
        if value not in dockerfile:
            errors.append(f"Dockerfile is missing required packaging declaration: {value}")
    for forbidden in ("COPY . .", "COPY tests/", "COPY docs/", "old-ach-agent"):
        if forbidden in dockerfile:
            errors.append(f"Dockerfile contains forbidden context copy/reference: {forbidden}")
    for value in (".git", ".env", "tests/", "node_modules/", "old-ach-agent", "__pycache__", "dist/"):
        if value not in ignore:
            errors.append(f".dockerignore is missing {value}")

    if "name: ach-runtime-seaweed-e2e" not in compose:
        errors.append("compose name must be ach-runtime-seaweed-e2e")
    if "services:" not in compose or "s3:" not in compose:
        errors.append("compose must define the s3 fixture service")
    for value in (
        "profiles: [fixture]",
        "image: chrislusf/seaweedfs:4.48",
        "entrypoint: [\"weed\"]",
        "- mini",
        "19000:8333",
        "cpus: 0.5",
        "mem_limit: 256m",
        "external: true",
        "ach-runtime-seaweed-e2e_test",
    ):
        if value not in compose:
            errors.append(f"compose is missing fixture declaration: {value}")
    if "build:" in compose or "control:" in compose or "execution:" in compose:
        errors.append("compose must contain only the existing storage fixture")
    for value in (
        'modes.add_argument("--images"',
        "def image_checks()",
        "def tcp_smoke()",
        "fake-facades.mjs",
        '"/healthz"',
        '"/configure"',
        '"/start"',
        '"/status/"',
        '"tools/call"',
        '"--pull", "never"',
        '"--time", "25"',
    ):
        if value not in driver:
            errors.append(f"image driver is missing required smoke gate: {value}")
    return errors


def docker(*args: str, timeout: float = 45, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], text=True, capture_output=True, timeout=timeout, check=False
    )
    if check and result.returncode:
        raise RuntimeError(f"docker {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def image_command(image: str, *args: str, timeout: float = 30,
                  entrypoint: str | None = None) -> str:
    name = f"ach-runtime-probe-{uuid.uuid4().hex[:12]}"
    command = [
        "run", "--name", name, "--pull", "never", "--network", "none", "--cpus", "0.5",
        "--memory", IMAGE_MEMORY, "--memory-swap", IMAGE_MEMORY,
    ]
    if entrypoint:
        command.extend(("--entrypoint", entrypoint))
    command.extend((image, *args))
    primary_error: BaseException | None = None
    try:
        result = docker(*command, timeout=timeout)
        return result.stdout.strip()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        termination_error: SystemExit | None = None
        try:
            removed = docker("rm", "-f", name, timeout=15, check=False)
            require(removed.returncode == 0,
                    f"probe container cleanup failed for {name}: {removed.stderr.strip()}")
        except BaseException as cleanup_error:
            if isinstance(cleanup_error, SystemExit):
                termination_error = cleanup_error
            elif primary_error is not None:
                primary_error.add_note(f"probe cleanup also failed: {cleanup_error}")
            else:
                raise RuntimeError(f"probe cleanup failed for {name}: {cleanup_error}") from cleanup_error
        if termination_error is not None:
            raise termination_error


def image_checks() -> None:
    image_ids: dict[str, str] = {}
    for image in (CONTROL_IMAGE, EXECUTION_IMAGE):
        require(bool(docker("image", "inspect", image, check=False).stdout), f"required local image missing: {image}")
        image_ids[image] = docker("image", "inspect", "--format", "{{.Id}}", image).stdout.strip()

    for image, command in (
        (CONTROL_IMAGE, ("--help",)),
        (CONTROL_IMAGE, ("control", "--help")),
        (EXECUTION_IMAGE, ("--help",)),
        (EXECUTION_IMAGE, ("execution", "--help")),
    ):
        image_command(image, *command)

    expected_defaults = {
        CONTROL_IMAGE: ["control", "--config", "/etc/ach-runtime/config.json"],
        EXECUTION_IMAGE: ["execution", "--config", "/etc/ach-runtime/bootstrap.json"],
    }
    for image, expected_cmd in expected_defaults.items():
        raw = docker("image", "inspect", "--format", "{{json .Config}}", image).stdout
        config = json.loads(raw)
        require(config.get("Entrypoint") == ["python", "-m", "ach_runtime"], f"wrong ENTRYPOINT in {image}")
        require(config.get("Cmd") == expected_cmd, f"wrong default command in {image}: {config.get('Cmd')!r}")
    print("PASS: image CLI entrypoint and default commands")

    dependency_probe = (
        "import importlib.metadata as m,json,sys; "
        "assert sys.version_info[:3] == (3,13,15), sys.version; "
        "v={n:m.version(n) for n in ('uvicorn','mcp','cryptography','boto3')}; "
        "assert v['uvicorn']=='0.50.2',v; "
        "assert tuple(map(int,v['mcp'].split('.')[:2])) >= (1,28),v; "
        "assert int(v['cryptography'].split('.')[0]) >= 44,v; "
        "assert tuple(map(int,v['boto3'].split('.')[:3])) >= (1,35,69),v; "
        "print(json.dumps(v,sort_keys=True))"
    )
    versions: dict[str, str] = {}
    for image in (CONTROL_IMAGE, EXECUTION_IMAGE):
        versions[image] = image_command(image, "-c", dependency_probe, entrypoint="python")
        image_command(image, "-c", "import ach_runtime,fastapi,httpx,pydantic", entrypoint="python")
        image_command(image, "-c", "import pathlib; assert pathlib.Path('/work/src/ach_runtime').is_dir(); assert pathlib.Path('/work/plugins/managed-config').is_dir(); assert not pathlib.Path('/work/tests').exists(); assert not pathlib.Path('/work/docs').exists(); assert not pathlib.Path('/work/old-ach-agent').exists()", entrypoint="python")
        image_command(image, "-c", "import subprocess; subprocess.run(['git','--version'],check=True); subprocess.run(['sh','-c','true'],check=True)", entrypoint="python")
        image_command(image, "-c", "import importlib.util,shutil; assert importlib.util.find_spec('pip') is None; assert shutil.which('pip') is None; assert shutil.which('pip3') is None", entrypoint="python")
    image_command(
        CONTROL_IMAGE,
        "-c",
        "import shutil; assert shutil.which('node') is None; assert shutil.which('npm') is None; assert shutil.which('opencode2') is None",
        entrypoint="python",
    )
    execution_binary_probe = (
        "node --version && /opt/opencode/node_modules/.bin/opencode2 --version "
        "&& test \"$OPENCODE_BIN\" = /opt/opencode/node_modules/.bin/opencode2 "
        "&& test ! -e /usr/bin/npm && test ! -e /usr/local/bin/npm "
        "&& test -d /opt/opencode/node_modules/@opencode/cli "
        "&& python -c 'import json; assert json.load(open(\"/opt/opencode/node_modules/@opencode/cli/package.json\"))[\"version\"] == \"2.0.20\"'"
    )
    binary_output = image_command(
        EXECUTION_IMAGE, "-c", f"import subprocess; subprocess.run(['sh','-ec',{execution_binary_probe!r}],check=True)",
        entrypoint="python",
    ).splitlines()
    require(any(line.strip() == "v22.23.3" for line in binary_output),
            f"unexpected Node runtime version: {binary_output!r}")
    require(any("2.0.20" in line for line in binary_output),
            f"OpenCode 2.0.20 did not execute: {binary_output!r}")
    print(f"PASS: control id={image_ids[CONTROL_IMAGE]} dependencies={versions[CONTROL_IMAGE]}")
    print(f"PASS: execution id={image_ids[EXECUTION_IMAGE]} dependencies={versions[EXECUTION_IMAGE]}")
    print(f"PASS: Node/OpenCode runtime={binary_output!r}; code/plugins and final filesystem checks")


def _public_wire() -> dict[str, Any]:
    # Reuse the same accepted config fixture and projection as test_boot.py. These host-side
    # imports only prepare the wire payload; the container receives no source/PYTHONPATH mount.
    sys.path.insert(0, str(ROOT / "src"))
    from ach_runtime.config.public import public_config
    from ach_runtime.config.schema import RuntimeConfig

    config = RuntimeConfig.model_validate_json(PUBLIC_FIXTURE.read_text(encoding="utf-8"))
    return public_config(config, engine_env=config.engine.env).model_dump(mode="json", by_alias=True)


def _auth(key: bytes, workspace: str, pod_uid: str) -> dict[str, str]:
    sys.path.insert(0, str(ROOT / "src"))
    from ach_runtime.harness.tokens import engine_bearer, engine_verify_key

    return {
        "Authorization": f"Bearer {engine_bearer(key, workspace)}",
        "X-ACH-Pod-UID": pod_uid,
    }


def _request(base: str, path: str, *, method: str = "GET", payload: Any = None,
             headers: dict[str, str] | None = None, timeout: float = 4) -> tuple[int, Any]:
    body = None if payload is None else json.dumps(payload).encode()
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=body, headers=request_headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None


def _wait_line(proc: subprocess.Popen[str], timeout: float) -> str:
    require(proc.stdout is not None, "fake facade helper has no stdout")
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError("timed out waiting for fake facade fixture")
    line = proc.stdout.readline()
    require(bool(line), f"fake facade helper exited early with {proc.poll()}")
    return line


def _fake_facades_script() -> str:
    # The public fixture's model type is gemini, so the managed plugin selects the native
    # Google provider. Answer :streamGenerateContent here; the spike still serves echo MCP.
    return r'''import { createServer } from "node:http"
import { startFakeFacades } from "/fixture/fake-facades.mjs"
const fakes = await startFakeFacades()
const requests = []
let echoCallSent = false
let finalPongSent = false
const model = createServer(async (req, res) => {
  const chunks = []
  for await (const chunk of req) chunks.push(chunk)
  let body = {}
  try { body = JSON.parse(Buffer.concat(chunks).toString("utf8")) } catch {}
  const contents = Array.isArray(body.contents) ? body.contents : []
  const record = {
    index: requests.length, url: req.url, lastMessage: contents.at(-1),
    toolNames: (body.tools ?? []).flatMap((tool) => tool.functionDeclarations ?? []).map((tool) => tool.name),
  }
  requests.push(record)
  if (!req.url.includes(":streamGenerateContent")) return res.writeHead(404).end()
  const toolResult = contents.some((content) => (content.parts ?? []).some((part) =>
    part.functionResponse && JSON.stringify(part.functionResponse).toLowerCase().includes("pong")))
  const toolsDisabled = body.toolConfig?.functionCallingConfig?.mode === "NONE"
  let parts = [{ text: "titling" }]
  if (echoCallSent && toolResult && !toolsDisabled) {
    finalPongSent = true
    parts = [{ text: "pong" }]
  } else if (record.toolNames.includes("execute") && !echoCallSent && !toolsDisabled) {
    echoCallSent = true
    parts = [{ functionCall: { name: "execute", args: {
      code: 'const catalog = search({namespace:"echo", query:"echo", limit:10}); const result = await tools.echo.echo({text:"pong"}); return {catalog, result};',
    } } }]
  }
  res.writeHead(200, { "content-type": "text/event-stream" })
  res.end(`data: ${JSON.stringify({ candidates: [{ content: { role: "model", parts }, finishReason: "STOP" }] })}\n\n`)
})
await new Promise((resolve) => model.listen(0, "127.0.0.1", resolve))
const modelUrl = `http://127.0.0.1:${model.address().port}/v1`
process.stdout.write(JSON.stringify({ready:true,modelUrl,mcpUrl:fakes.mcpUrl}) + "\n")
for await (const chunk of process.stdin) {
  if (chunk.toString().trim() !== "snapshot") continue
  process.stdout.write(JSON.stringify({requests,mcpRequests:fakes.mcpRequests,finalPongSent}) + "\n")
  await Promise.all([fakes.close(), new Promise((resolve) => { model.closeAllConnections?.(); model.close(resolve) })])
  break
}
'''


def _start_fake_helper(image: str, container: str, helper_name: str,
                       generated: Path) -> subprocess.Popen[str]:
    generated.write_text(_fake_facades_script(), encoding="utf-8")
    cmd = [
        "docker", "run", "-i", "--pull", "never", "--name", helper_name,
        "--network", f"container:{container}", "--cpus", "0.25", "--memory", "256m",
        "--memory-swap", "256m", "--mount", f"type=bind,src={FACADE_FIXTURE},dst=/fixture/fake-facades.mjs,readonly",
        "--mount", f"type=bind,src={generated},dst=/tmp/runtime-fake-facades.mjs,readonly",
        "--entrypoint", "node", image, "/tmp/runtime-fake-facades.mjs",
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)


def tcp_smoke() -> None:
    require(PUBLIC_FIXTURE.is_file() and BOOT_FIXTURE.is_file() and FACADE_FIXTURE.is_file(),
            "required existing boot/model/MCP fixtures are missing")
    key = b"runtime-image-smoke-key-material".ljust(32, b"!")
    workspace_name, pod_uid = "gitlab-reviewer", "runtime-image-smoke-pod"
    workspace_ref, session_ref = "ws" * 16, "se" * 16
    headers = _auth(key, workspace_name, pod_uid)
    # Build the exact public projection used by the existing TCP boot test.
    public = _public_wire()
    bootstrap = json.loads(BOOT_FIXTURE.read_text(encoding="utf-8"))
    require(public["infrastructure"]["facadeEndpoint"] == bootstrap["facadeEndpoint"],
            "boot and public fixtures have mismatched facade endpoints")

    run_id = uuid.uuid4().hex[:12]
    container = f"ach-runtime-image-{run_id}"
    helper_name = f"ach-runtime-facades-{run_id}"
    with tempfile.TemporaryDirectory(prefix="ach-runtime-images-") as temp:
        tempdir = Path(temp)
        config_path = tempdir / "bootstrap.json"
        config_path.write_text(json.dumps(bootstrap), encoding="utf-8")
        create_args = [
            "create", "--pull", "never", "--name", container, "--cpus", "1",
            "--memory", IMAGE_MEMORY, "--memory-swap", IMAGE_MEMORY,
            "--publish", "127.0.0.1::8080",
            "--mount", f"type=bind,src={config_path},dst=/etc/ach-runtime/bootstrap.json,readonly",
            "--mount", "type=volume,dst=/workspace,volume-nocopy",
            "--mount", "type=volume,dst=/var/lib/ach-runtime/sessions,volume-nocopy",
            "--env", f"ACH_SANDBOX_VERIFY_KEY={_verify_key(key)}",
            "--env", f"ACH_WORKSPACE_NAME={workspace_name}",
            "--env", f"ACH_WORKSPACE_REF={workspace_ref}",
            "--env", f"ACH_POD_UID={pod_uid}", EXECUTION_IMAGE,
        ]
        fixture_proc: subprocess.Popen[str] | None = None
        shutdown_elapsed: float | None = None
        cleanup_errors: list[str] = []
        primary_error: BaseException | None = None
        termination_error: SystemExit | None = None
        try:
            docker(*create_args)
            docker("start", container)
            port_text = docker("port", container, "8080/tcp").stdout.strip()
            host_port = int(port_text.rsplit(":", 1)[1])
            base = f"http://127.0.0.1:{host_port}"
            fixture_proc = _start_fake_helper(
                EXECUTION_IMAGE, container, helper_name, tempdir / "fake_facades.mjs"
            )
            ready_line = _wait_line(fixture_proc, 15)
            fakes = json.loads(ready_line)
            require(fakes.get("ready") is True, "fake facades did not report ready")

            deadline = time.monotonic() + 15
            while True:
                try:
                    status, _ = _request(base, "/healthz")
                    if status == 200:
                        break
                except (OSError, urllib.error.URLError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("execution image did not open health TCP endpoint")
                time.sleep(0.1)
            require(_request(base, "/readyz")[0] == 503, "new mini must be cold (ready 503)")
            require(_request(base, "/runs")[0] == 401, "unauthorized request must return 401")
            require(_request(base, "/configure", method="POST", payload={}, headers=headers)[0] == 422,
                    "malformed authorized configure must return 422")

            catalog = {
                "providers": {public["model"]["type"]: {
                    "baseURL": fakes["modelUrl"], "apiKey": "local-proxy",
                    "models": {public["model"]["name"]: {}},
                }},
                "mcp": {"echo": {"url": fakes["mcpUrl"], "headers": {}}},
            }
            configured = _request(base, "/configure", method="POST",
                                  payload={"public": public, "catalog": catalog}, headers=headers)
            require(configured[0] == 200, f"valid configure failed: {configured}")
            require(_request(base, "/readyz")[0] == 200, "configured mini must be ready")
            invocation = "runtime-image-pong"
            request = {
                "invocationId": invocation, "channel": "webhook", "workspaceRef": workspace_ref,
                "sessionRef": session_ref, "configVersion": public["configVersion"],
                "prompt": "Call the echo MCP tool with text pong, then reply with exactly pong.",
                "deadlineAt": "2099-01-01T00:00:00+00:00",
            }
            started = _request(base, "/start", method="POST", payload=request, headers=headers)
            require(started[0] == 202, f"Start was not accepted: {started}")
            require(started[1]["invocationId"] == invocation, "Started invocation mismatch")
            require(started[1]["sessionRef"] == session_ref and
                    started[1]["configVersion"] == public["configVersion"],
                    f"Started identity/configuration mismatch: {started[1]}")
            end = time.monotonic() + 150
            status_wire: Any = None
            while time.monotonic() < end:
                code, status_wire = _request(base, f"/status/{invocation}", headers=headers)
                require(code == 200, f"status request failed: {status_wire}")
                if status_wire["state"] in {"completed", "failed", "cancelled", "unknown"}:
                    break
                time.sleep(0.25)
            require(status_wire and status_wire["state"] == "completed",
                    f"invocation did not complete successfully: {status_wire}")
            require(status_wire["workspaceRef"] == workspace_ref and
                    status_wire["sessionRef"] == session_ref and
                    status_wire["configVersion"] == public["configVersion"],
                    f"terminal status identity/configuration mismatch: {status_wire}")
            listed = _request(base, "/runs", headers=headers)
            require(listed[0] == 200 and any(r.get("invocationId") == invocation for r in listed[1]),
                    "completed invocation missing from run list")

            require(fixture_proc.stdin is not None, "fake facade helper has no stdin")
            fixture_proc.stdin.write("snapshot\n")
            fixture_proc.stdin.flush()
            evidence = json.loads(_wait_line(fixture_proc, 5))
            requests = evidence["requests"]
            mcp = evidence["mcpRequests"]
            methods = {entry.get("method") for entry in mcp}
            require({"initialize", "notifications/initialized", "tools/list", "tools/call"} <= methods,
                    f"MCP handshake/call evidence incomplete: {sorted(methods)}")
            calls = [entry for entry in mcp if entry.get("method") == "tools/call"]
            require(any(c.get("params", {}).get("name") == "echo" and
                        c.get("params", {}).get("arguments", {}).get("text") == "pong" for c in calls),
                    "native MCP echo call did not carry text=pong")
            returned = [r for r in requests if r.get("index", 0) > 0 and
                        "pong" in json.dumps(r.get("lastMessage", {})).lower()]
            require(evidence.get("finalPongSent") is True and len(requests) >= 2 and returned,
                    "model did not receive the native MCP pong result before its final response")
            print("PASS: mini TCP cold/configure/start/status; native model/MCP echo=pong; terminal=completed")
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if fixture_proc is not None:
                try:
                    if fixture_proc.stdin is not None and not fixture_proc.stdin.closed:
                        fixture_proc.stdin.close()
                except BaseException as exc:
                    if isinstance(exc, SystemExit):
                        termination_error = exc
                    cleanup_errors.append(f"helper stdin close failed: {exc}")
                try:
                    try:
                        fixture_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        fixture_proc.terminate()
                        try:
                            fixture_proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            fixture_proc.kill()
                            fixture_proc.wait(timeout=3)
                except BaseException as exc:
                    if isinstance(exc, SystemExit):
                        termination_error = exc
                    cleanup_errors.append(f"helper process wait/termination failed: {exc}")
            try:
                removed = docker("rm", "-f", helper_name, timeout=15, check=False)
                if removed.returncode:
                    cleanup_errors.append(f"helper container removal failed: {removed.stderr.strip()}")
            except BaseException as exc:
                if isinstance(exc, SystemExit):
                    termination_error = exc
                cleanup_errors.append(f"helper container removal failed: {exc}")

            try:
                before = json.loads(docker("inspect", "--format", "{{json .State}}", container,
                                           timeout=15).stdout)
                require(before.get("Running") is True,
                        f"execution container was not running before shutdown: {before}")
                started_at = time.monotonic()
                stopped = docker("stop", "--time", "25", container, timeout=32, check=False)
                shutdown_elapsed = time.monotonic() - started_at
                require(stopped.returncode == 0,
                        f"execution container failed to stop cleanly: {stopped.stderr.strip()}")
                state = json.loads(docker("inspect", "--format", "{{json .State}}", container,
                                          timeout=15).stdout)
                require(state.get("ExitCode") == 0 and state.get("OOMKilled") is False,
                        f"execution container exited abnormally: {state}")
                require(shutdown_elapsed <= 25,
                        f"execution image shutdown exceeded 25s: {shutdown_elapsed:.2f}s")
                print(f"PASS: execution image SIGTERM shutdown in {shutdown_elapsed:.2f}s (<=25s); exit=0; oom=false")
            except BaseException as exc:
                if isinstance(exc, SystemExit):
                    termination_error = exc
                cleanup_errors.append(f"execution shutdown verification failed: {exc}")
            try:
                removed = docker("rm", "-f", "-v", container, timeout=15, check=False)
                if removed.returncode:
                    cleanup_errors.append(f"execution container removal failed: {removed.stderr.strip()}")
            except BaseException as exc:
                if isinstance(exc, SystemExit):
                    termination_error = exc
                cleanup_errors.append(f"execution container removal failed: {exc}")
            if termination_error is not None:
                if primary_error is not None:
                    termination_error.add_note(f"interrupted while cleaning up after: {primary_error}")
                if cleanup_errors:
                    termination_error.add_note("smoke cleanup failure(s): " + "; ".join(cleanup_errors))
                raise termination_error
            if cleanup_errors:
                message = "; ".join(cleanup_errors)
                if primary_error is not None:
                    primary_error.add_note(f"smoke cleanup failure(s): {message}")
                else:
                    raise RuntimeError(f"smoke cleanup failure(s): {message}")


def _verify_key(key: bytes) -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from ach_runtime.harness.tokens import engine_verify_key

    return engine_verify_key(key)


def images() -> None:
    image_checks()
    tcp_smoke()


def main() -> int:
    def terminate(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="check packaging declarations")
    modes.add_argument("--images", action="store_true", help="run final-image smoke")
    args = parser.parse_args()
    try:
        if args.check:
            errors = check()
            if errors:
                print("FAIL: static runtime image checks")
                for error in errors:
                    print(f"- {error}")
                return 1
            print("PASS: static runtime image checks")
            return 0
        images()
    except Exception as exc:  # noqa: BLE001 - standalone command reports a concise gate failure
        print(f"FAIL: runtime image smoke: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Semantic start/stop/restart operator for the six production processes."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit
import urllib.error
import urllib.request
from uuid import uuid4


_SERVICES = ("gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n")
_ACTIONS = ("start", "stop", "restart")
_INHERITED_ENV = ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")
_SERVICE_SECRET_ENV = {
    "gateway": (
        "CAPTAIN_GATEWAY_TOKEN", "WORKER_GATEWAY_TOKEN",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN", "CAPTAIN_RUNTIME_TOKEN",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN", "MINIBOOK_API_KEY",
        "MINIBOOK_PROJECTION_API_KEY",
    ),
    "runtime": (
        "CAPTAIN_RUNTIME_TOKEN", "CAPTAIN_GATEWAY_TOKEN",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
    ),
    "provider_proxy": (
        "OPENAI_API_KEY", "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN",
        "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN", "CAPTAIN_RUNTIME_TOKEN",
    ),
    "benchmark": ("CAPTAIN_CAPABILITY_BENCHMARK_TOKEN",),
    "minibook": ("MINIBOOK_API_KEY", "MINIBOOK_PROJECTION_API_KEY"),
    "n8n": ("CAPTAIN_N8N_API_KEY",),
}
_SERVICE_CONFIG_ENV = {
    "gateway": (
        "LEDGER_DSN", "GATEWAY_HOST", "GATEWAY_PORT", "GATEWAY_CLAIM_TTL_SECONDS",
        "CAPTAIN_GATEWAY_URL", "CAPTAIN_RUNTIME_URL", "CAPTAIN_N8N_URL",
        "CAPTAIN_CAPABILITY_BENCHMARK_URL", "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID",
        "CAPTAIN_CAPABILITY_ARTIFACT_ROOT", "CAPTAIN_CAPABILITY_AUTHORITY_ROOT",
        "CAPTAIN_CAPABILITY_ASSEMBLY_REPOSITORY_REVISION",
        "CAPTAIN_CAPABILITY_ASSEMBLY_INPUTS_JSON", "CAPTAIN_CAPABILITY_ASSEMBLY_TOOL_GAPS_JSON",
        "CAPTAIN_CAPABILITY_ASSEMBLY_SUBJECT_VERSION", "CAPTAIN_PRIVATE_BENCHMARK_ROOT",
        "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_PATH", "CAPTAIN_PRIVATE_BENCHMARK_MANIFEST_SHA256",
        "CAPTAIN_CAPABILITY_FACTORY_ADAPTER", "CAPTAIN_CAPABILITY_FACTORY_ADAPTER_SHA256",
        "CAPTAIN_CAPABILITY_FACTORY_DESCRIPTOR", "CAPTAIN_CAPABILITY_FACTORY_PLAN_SHA256",
        "CAPTAIN_CAPABILITY_EXECUTION_POLICY_JSON", "CAPTAIN_BENCHMARK_MODEL",
        "CAPTAIN_BENCHMARK_PROVIDER", "CAPTAIN_PROVIDER_PROXY_URL",
        "CAPTAIN_PROVIDER_PROXY_RESPONSES_URL", "CAPTAIN_PROVIDER_POLICY_SHA256",
        "CAPTAIN_PROVIDER_PRICE_CARD_SHA256", "CAPTAIN_PROVIDER_CONTEXT_SHA256",
        "CAPTAIN_PROVIDER_PROXY_EXECUTABLE_SHA256", "CAPTAIN_PROVIDER_PROXY_CODE_SHA256",
        "CAPTAIN_PROVIDER_PAID_COMMAND_SHA256", "CAPTAIN_PROVIDER_HARD_CEILING_ENFORCED",
        "MINIBOOK_BACKEND_URL", "CAPTAIN_CAPABILITY_ALLOWED_HOSTS",
    ),
    "runtime": (
        "CAPTAIN_RUNTIME_URL", "CAPTAIN_RUNTIME_PORT", "CAPTAIN_GATEWAY_URL",
        "CAPTAIN_PROVIDER_PROXY_URL", "CAPTAIN_RUNTIME_ADAPTER_MANIFEST",
        "CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256", "CAPTAIN_CODEX_MODEL",
        "CAPTAIN_CODEX_REASONING_EFFORT", "CAPTAIN_CODEX_EXECUTABLE",
    ),
    "provider_proxy": (
        "CAPTAIN_PROVIDER_PROXY_URL", "CAPTAIN_PROVIDER_PRICE_CARD_PATH",
        "CAPTAIN_PROVIDER_POLICY_PATH", "CAPTAIN_PROVIDER_STATE_ROOT",
        "CAPTAIN_GATEWAY_URL",
    ),
    "benchmark": (
        "CAPTAIN_CAPABILITY_BENCHMARK_URL", "CAPTAIN_CAPABILITY_BENCHMARK_PORT",
        "CAPTAIN_CAPABILITY_BENCHMARK_AUTHORITY_ID", "CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR",
        "CAPTAIN_CAPABILITY_BENCHMARK_EXECUTOR_SHA256",
    ),
    "minibook": ("MINIBOOK_BACKEND_URL",),
    "n8n": ("CAPTAIN_N8N_URL",),
}


def _service_environment(service: str, environment: Mapping[str, str]) -> dict[str, str]:
    allowed = (*_INHERITED_ENV, *_SERVICE_SECRET_ENV[service], *_SERVICE_CONFIG_ENV[service])
    child = {name: environment[name] for name in allowed if environment.get(name, "").strip()}
    for name in _SERVICE_SECRET_ENV[service]:
        if name not in child:
            raise ValueError(f"required process setting is missing: {name}")
    child["PYTHONUTF8"] = "1"
    return child


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate one Captain production process")
    parser.add_argument("--service", choices=_SERVICES, required=True)
    parser.add_argument("action", choices=_ACTIONS)
    return parser


def _state_root(environment: Mapping[str, str], repository_root: Path) -> Path:
    raw = environment.get("CAPTAIN_PROCESS_STATE_ROOT", ".captain-cook/processes")
    candidate = Path(raw)
    root = repository_root.resolve(strict=True)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("process state root is outside the repository") from None
    return resolved


def _read_state(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("process state is invalid")
    return value


def _write_state(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(temporary, path)


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _observed_process_identity(pid: int) -> tuple[str, str] | None:
    """Read the kernel-owned executable/start identity for PID-reuse protection."""

    if not _pid_is_live(pid):
        return None
    if os.name != "nt":
        try:
            executable = str(Path(f"/proc/{pid}/exe").resolve(strict=True))
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            return executable, fields[21]
        except (OSError, IndexError):
            return None
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        query = kernel32.QueryFullProcessImageNameW
        query.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        )
        query.restype = wintypes.BOOL
        if not query(process, 0, buffer, ctypes.byref(size)):
            return None
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        get_times = kernel32.GetProcessTimes
        get_times.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        get_times.restype = wintypes.BOOL
        if not get_times(
            process,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        started = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return str(Path(buffer.value).resolve()), str(started)
    finally:
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(process)


def _require_same_process(state: Mapping[str, object]) -> int:
    pid = int(state.get("process_id", 0))
    observed = _observed_process_identity(pid)
    expected = (state.get("executable"), state.get("os_start_identity"))
    if observed is None or observed != expected:
        raise RuntimeError("managed process identity changed; refusing to stop PID")
    return pid


def _observed_n8n_container_identity() -> tuple[str, str] | None:
    inventory = subprocess.run(
        (
            "docker", "ps", "-q",
            "--filter", "label=com.docker.compose.project=captain-n8n-builder",
            "--filter", "label=com.docker.compose.service=n8n",
            "--filter", "status=running",
        ), capture_output=True, text=True, check=False, timeout=10,
    )
    ids = tuple(line.strip() for line in inventory.stdout.splitlines() if line.strip())
    if inventory.returncode != 0 or len(ids) != 1:
        return None
    inspected = subprocess.run(
        ("docker", "inspect", "--format", "{{.Id}}|{{.State.StartedAt}}", ids[0]),
        capture_output=True, text=True, check=False, timeout=10,
    )
    values = inspected.stdout.strip().split("|", 1)
    if inspected.returncode != 0 or len(values) != 2 or not all(values):
        return None
    return values[0], values[1]


def _wait_process_gone(state: Mapping[str, object], *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _observed_process_identity(int(state["process_id"])) is None:
            return True
        time.sleep(0.05)
    return _observed_process_identity(int(state["process_id"])) is None


def _terminate_exact_process_tree(state: Mapping[str, object]) -> bool:
    """Terminate only the still-matching kernel process and confirm its exit."""

    pid = _require_same_process(state)
    os.kill(pid, signal.SIGTERM)
    if _wait_process_gone(state, timeout_seconds=5.0):
        return True
    pid = _require_same_process(state)
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(pid), "/T", "/F"),
            capture_output=True,
            check=False,
            timeout=10,
        )
    else:
        os.kill(pid, signal.SIGKILL)
    return _wait_process_gone(state, timeout_seconds=2.0)


def _require_same_n8n_container(state: Mapping[str, object]) -> str:
    observed = _observed_n8n_container_identity()
    expected = str(state.get("identity_value", ""))
    if observed is None or f"{observed[0]}:{observed[1]}" != expected:
        raise RuntimeError("managed n8n container identity changed; refusing to stop")
    return observed[0]


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required process setting is missing: {name}")
    return value


def _wait_authenticated_health(
    service: str, *, environment: Mapping[str, str], timeout_seconds: float = 30.0,
) -> None:
    definitions = {
        "gateway": ("CAPTAIN_GATEWAY_URL", "/v1/capability-assemblies/health", "CAPTAIN_GATEWAY_TOKEN", "Authorization"),
        "runtime": ("CAPTAIN_RUNTIME_URL", "/health", "CAPTAIN_RUNTIME_TOKEN", "Authorization"),
        "provider_proxy": ("CAPTAIN_PROVIDER_PROXY_URL", "/v1/healthz", "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN", "Authorization"),
        "benchmark": ("CAPTAIN_CAPABILITY_BENCHMARK_URL", "/healthz", "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN", "Authorization"),
        "minibook": ("MINIBOOK_BACKEND_URL", "/api/v1/projection-health", "MINIBOOK_PROJECTION_API_KEY", "Authorization"),
        "n8n": ("CAPTAIN_N8N_URL", "/api/v1/workflows?limit=1", "CAPTAIN_N8N_API_KEY", "X-N8N-API-KEY"),
    }
    url_name, suffix, token_name, header = definitions[service]
    base = _required(environment, url_name)
    if service == "benchmark":
        base = base.removesuffix("/v1/execute")
    token = _required(environment, token_name)
    value = token if header == "X-N8N-API-KEY" else f"Bearer {token}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        request = urllib.request.Request(base.rstrip("/") + suffix, headers={header: value})
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"authenticated health timed out for {service}")


def service_argv(
    service: str, *, environment: Mapping[str, str], repository_root: Path,
    action: str = "start",
) -> tuple[str, ...]:
    """Return the real service command without starting it."""

    python = sys.executable
    if service == "gateway":
        return (python, "-m", "gateway.app")
    if service == "runtime":
        return (python, "-m", "agenten.agent_runtime.runtime_entrypoint")
    if service == "benchmark":
        return (python, "-m", "gateway.capability_benchmark_process_app")
    if service == "provider_proxy":
        provider_url = _required(environment, "CAPTAIN_PROVIDER_PROXY_URL")
        parsed = urlsplit(provider_url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port is None:
            raise ValueError("provider proxy URL must bind one loopback port")
        values = (
            "--price-card", _required(environment, "CAPTAIN_PROVIDER_PRICE_CARD_PATH"),
            "--policy", _required(environment, "CAPTAIN_PROVIDER_POLICY_PATH"),
            "--state-root", _required(environment, "CAPTAIN_PROVIDER_STATE_ROOT"),
            "--gateway-url", _required(environment, "CAPTAIN_GATEWAY_URL"),
            "--port", str(parsed.port),
        )
        return (python, "-m", "agenten.provider_proxy.entrypoint", *values)
    if service == "minibook":
        return (python, "-m", "minibook.src.main")
    if service == "n8n":
        if action not in _ACTIONS:
            raise ValueError("unsupported n8n operation")
        return (
            "pwsh", "-NoProfile", "-File",
            str((repository_root / "scripts" / "captain-n8n.ps1").resolve()),
            "start" if action in {"start", "restart"} else "stop",
        )
    raise ValueError("unsupported process service")


def operate(
    service: str,
    action: str,
    *,
    environment: Mapping[str, str],
    repository_root: Path,
) -> dict[str, object]:
    if service not in _SERVICES or action not in _ACTIONS:
        raise ValueError("unsupported process operation")
    state_file = _state_root(environment, repository_root) / f"{service}.json"
    prior = _read_state(state_file)
    sentinel = environment.get("CAPTAIN_PROCESS_OPERATOR_BACKEND") == "sentinel"

    if action in {"stop", "restart"}:
        if not sentinel and service == "n8n" and prior is not None:
            container_id = _require_same_n8n_container(prior)
            completed = subprocess.run(
                service_argv(
                    service,
                    environment=environment,
                    repository_root=repository_root,
                    action="stop",
                ),
                cwd=repository_root,
                check=False,
                timeout=30,
            )
            if completed.returncode != 0:
                raise RuntimeError("n8n process stop failed")
            deadline = time.monotonic() + 5.0
            while container_id is not None and time.monotonic() < deadline:
                if _observed_n8n_container_identity() is None:
                    break
                time.sleep(0.1)
            if container_id is not None and _observed_n8n_container_identity() is not None:
                subprocess.run(("docker", "kill", container_id), check=False, timeout=10)
                if _observed_n8n_container_identity() is not None:
                    raise RuntimeError("n8n container exit could not be confirmed")
        if prior is not None:
            raw_pid = prior.get("process_id")
            pid = int(raw_pid) if raw_pid is not None else 0
            if not sentinel and service != "n8n" and _pid_is_live(pid):
                if not _terminate_exact_process_tree(prior):
                    raise RuntimeError("managed process exit could not be confirmed")
    if action in {"stop", "restart"} and prior is not None:
        stopped = {
            **prior,
            "status": "stopped",
            "operation": "stop",
        }
        _write_state(state_file, stopped)
        prior = stopped
    if action == "stop":
        return prior or {
            "schema": "captain.production-process-state.v1",
            "service": service,
            "status": "stopped",
            "operation": "stop",
            "process_id": None,
            "process_identity": "absent",
        }

    argv = service_argv(
        service, environment=environment, repository_root=repository_root, action="start"
    )
    current = _read_state(state_file)
    if action == "start" and current is not None and current.get("status") == "running":
        if sentinel:
            return current
        if service == "n8n":
            _require_same_n8n_container(current)
        else:
            _require_same_process(current)
        _wait_authenticated_health(service, environment=environment, timeout_seconds=5.0)
        return current
    identity = str(uuid4())
    if sentinel:
        process_id = int(identity.replace("-", "")[:8], 16) or 1
    elif service == "n8n":
        completed = subprocess.run(argv, cwd=repository_root, check=False, timeout=30)
        if completed.returncode != 0:
            raise RuntimeError("n8n process start failed")
        observed_container = _observed_n8n_container_identity()
        if observed_container is None:
            raise RuntimeError("started n8n container identity is unavailable")
        process_id = None
    else:
        logs = _state_root(environment, repository_root) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        output = (logs / f"{service}.log").open("ab")
        process = subprocess.Popen(
            argv,
            cwd=repository_root,
            env=_service_environment(service, environment),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt" else 0
            ),
        )
        process_id = process.pid
        output.close()
        observed_identity = _observed_process_identity(process_id)
        if observed_identity is None:
            process.terminate()
            raise RuntimeError("started process identity is unavailable")
    state = {
        "schema": "captain.production-process-state.v1",
        "service": service,
        "status": "running",
        "operation": action,
        "process_id": process_id,
        "process_identity": identity,
        "identity_kind": "sentinel" if sentinel else (
            "docker_container" if service == "n8n" else "os_process"
        ),
        "argv": list(argv),
    }
    if not sentinel and service != "n8n":
        state["executable"], state["os_start_identity"] = observed_identity
        state["identity_value"] = f"{observed_identity[0]}:{observed_identity[1]}"
    elif not sentinel:
        state["identity_value"] = f"{observed_container[0]}:{observed_container[1]}"
    else:
        state["identity_value"] = identity
    if not sentinel:
        try:
            _wait_authenticated_health(service, environment=environment)
            if service == "n8n":
                _require_same_n8n_container(state)
            else:
                _require_same_process(state)
        except Exception:
            gone = False
            try:
                if service == "n8n":
                    container_id = _require_same_n8n_container(state)
                    subprocess.run(
                        ("docker", "kill", container_id), check=False, timeout=10
                    )
                    gone = _observed_n8n_container_identity() is None
                else:
                    gone = _terminate_exact_process_tree(state)
            except Exception:
                # The exact authority could not be proven. Preserve the running
                # record so a later operator cannot adopt or silently forget it.
                gone = False
            if gone:
                state["status"] = "stopped"
            _write_state(state_file, state)
            raise RuntimeError(
                f"authenticated start failed for {service}"
            ) from None
    _write_state(state_file, state)
    return state


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = operate(
        args.service,
        args.action,
        environment=os.environ,
        repository_root=Path.cwd(),
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

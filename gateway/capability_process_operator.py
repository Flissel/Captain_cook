"""Semantic start/stop/restart operator for the six production processes."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
import urllib.error
import urllib.request
from uuid import uuid4


_SERVICES = ("gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n")
_ACTIONS = ("start", "stop", "restart")
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_INHERITED_ENV = ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP")
_SERVICE_SECRET_ENV = {
    "gateway": (
        "CAPTAIN_GATEWAY_TOKEN", "WORKER_GATEWAY_TOKEN",
        "CAPTAIN_CAPABILITY_BENCHMARK_TOKEN", "CAPTAIN_RUNTIME_TOKEN",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN", "MINIBOOK_API_KEY",
        "MINIBOOK_PROJECTION_API_KEY", "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN",
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
    "minibook": (
        "MINIBOOK_BACKEND_URL", "MINIBOOK_CREATION_DB",
        "MINIBOOK_PROJECTION_PROJECT_ID", "MINIBOOK_PROJECTION_SERVICE_AGENT_ID",
    ),
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


def _process_introspection_environment(
    environment: Mapping[str, str],
) -> dict[str, str]:
    child = {
        name: environment[name]
        for name in _INHERITED_ENV
        if environment.get(name, "").strip()
    }
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


@contextmanager
def _exclusive_state_lock(state_file: Path):
    """Serialize state reconciliation and effects across threads and processes."""

    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.with_suffix(".lock")
    key = str(lock_path.resolve())
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(key, threading.Lock())
    with local_lock, lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def _observed_n8n_container_identity(
    environment: Mapping[str, str],
) -> tuple[str, str] | None:
    docker_environment = _service_environment("n8n", environment)
    inventory = subprocess.run(
        (
            "docker", "ps", "-q",
            "--filter", "label=com.docker.compose.project=captain-n8n-builder",
            "--filter", "label=com.docker.compose.service=n8n",
            "--filter", "status=running",
        ), capture_output=True, text=True, check=False, timeout=10,
        env=docker_environment,
    )
    ids = tuple(line.strip() for line in inventory.stdout.splitlines() if line.strip())
    if inventory.returncode != 0 or len(ids) != 1:
        return None
    inspected = subprocess.run(
        ("docker", "inspect", "--format", "{{.Id}}|{{.State.StartedAt}}", ids[0]),
        capture_output=True, text=True, check=False, timeout=10,
        env=docker_environment,
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


def _collect_exact_process_tree(
    state: Mapping[str, object],
) -> tuple[tuple[int, str, str], ...]:
    root = _require_same_process(state)
    identities = [(root, str(state["executable"]), str(state["os_start_identity"]))]
    if os.name != "nt":
        return tuple(identities)
    command = (
        "$r=" + str(root) + ";$p=Get-CimInstance Win32_Process;"
        "$ids=@($r);do{$n=@($p|Where-Object{$ids-contains $_.ParentProcessId}"
        "|ForEach-Object ProcessId|Where-Object{$ids-notcontains $_});$ids+=$n}"
        "while($n.Count);$ids|ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ("pwsh", "-NoProfile", "-Command", command),
            capture_output=True, text=True, check=False, timeout=10,
            env=_process_introspection_environment(os.environ),
        )
        if result.returncode != 0:
            raise RuntimeError("managed process tree is unavailable")
        values = json.loads(result.stdout)
        pids = values if isinstance(values, list) else [values]
        if root not in {int(raw) for raw in pids}:
            raise RuntimeError("managed process tree is unavailable")
        for raw in pids:
            pid = int(raw)
            if pid == root:
                continue
            observed = _observed_process_identity(pid)
            if observed is not None:
                identities.append((pid, observed[0], observed[1]))
    except (OSError, ValueError, TypeError, json.JSONDecodeError,
            subprocess.SubprocessError):
        raise RuntimeError("managed process tree is unavailable") from None
    return tuple(identities)


def _tracked_tree_is_gone(tree: tuple[tuple[int, str, str], ...]) -> bool:
    return all(_observed_process_identity(pid) != (executable, started)
               for pid, executable, started in tree)


def _wait_tracked_tree_gone(
    tree: tuple[tuple[int, str, str], ...], *, timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _tracked_tree_is_gone(tree):
            return True
        time.sleep(0.05)
    return _tracked_tree_is_gone(tree)


def _terminate_exact_process_tree(state: Mapping[str, object]) -> bool:
    """Terminate only the still-matching kernel process and confirm its exit."""

    tree = _collect_exact_process_tree(state)
    pid = tree[0][0]
    os.kill(pid, signal.SIGTERM)
    if _wait_tracked_tree_gone(tree, timeout_seconds=5.0):
        return True
    for tracked_pid, executable, started in reversed(tree):
        if _observed_process_identity(tracked_pid) != (executable, started):
            continue
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(tracked_pid), "/T", "/F"),
                capture_output=True, check=False, timeout=10,
            )
        else:
            os.kill(tracked_pid, signal.SIGKILL)
    return _wait_tracked_tree_gone(tree, timeout_seconds=2.0)


def _require_same_n8n_container(
    state: Mapping[str, object], environment: Mapping[str, str],
) -> str:
    observed = _observed_n8n_container_identity(environment)
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
    request = _authenticated_health_request(service, environment=environment)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=2.0) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"authenticated health timed out for {service}")


def _authenticated_health_request(
    service: str, *, environment: Mapping[str, str],
) -> urllib.request.Request:
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
        if not base.endswith("/v1/execute"):
            raise ValueError("authenticated health endpoint is invalid")
        base = base.removesuffix("/v1/execute")
    parsed = urlsplit(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or parsed.path.rstrip("/")
    ):
        raise ValueError("authenticated health endpoint is invalid")
    token = _required(environment, token_name)
    value = token if header == "X-N8N-API-KEY" else f"Bearer {token}"
    return urllib.request.Request(base.rstrip("/") + suffix, headers={header: value})


def _preflight_operation(
    service: str, action: str, *, environment: Mapping[str, str], repository_root: Path,
) -> None:
    if service not in _SERVICES or action not in _ACTIONS:
        raise ValueError("unsupported process operation")
    _authenticated_health_request(service, environment=environment)
    if service == "minibook":
        _required(environment, "MINIBOOK_CREATION_DB")
    service_argv(
        service, environment=environment, repository_root=repository_root,
        action="start" if action == "restart" else action,
    )
    _service_environment(service, environment)


def _reconcile_starting(
    service: str,
    state: dict[str, object],
    *,
    environment: Mapping[str, str],
    state_file: Path,
) -> dict[str, object]:
    """Resolve one exclusive starting claim without ever adopting or respawning."""

    if state.get("identity_kind") == "sentinel":
        raise RuntimeError("managed process start is ambiguous")
    if service == "n8n":
        if not isinstance(state.get("identity_value"), str):
            raise RuntimeError("managed n8n start is ambiguous")
        try:
            container_id = _require_same_n8n_container(state, environment)
        except RuntimeError:
            raise RuntimeError("managed n8n start is ambiguous") from None
        try:
            _wait_authenticated_health(service, environment=environment, timeout_seconds=5.0)
        except Exception:
            subprocess.run(
                ("docker", "kill", container_id), check=False, timeout=10,
                env=_service_environment("n8n", environment),
            )
            if _observed_n8n_container_identity(environment) is None:
                state["status"] = "stopped"
                _write_state(state_file, state)
            raise RuntimeError("authenticated start failed for n8n") from None
    else:
        if not all(state.get(name) for name in (
            "process_id", "executable", "os_start_identity",
        )):
            raise RuntimeError("managed process start is ambiguous")
        try:
            _require_same_process(state)
        except RuntimeError:
            raise RuntimeError("managed process start is ambiguous") from None
        try:
            _wait_authenticated_health(service, environment=environment, timeout_seconds=5.0)
        except Exception:
            gone = _terminate_exact_process_tree(state)
            if gone:
                state["status"] = "stopped"
                _write_state(state_file, state)
            raise RuntimeError(f"authenticated start failed for {service}") from None
    state["status"] = "running"
    _write_state(state_file, state)
    return state


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


def _operate_locked(
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
    if prior is not None and prior.get("status") == "starting":
        prior = _reconcile_starting(
            service, prior, environment=environment, state_file=state_file,
        )

    if action in {"stop", "restart"}:
        if not sentinel and service == "n8n" and prior is not None:
            container_id = _require_same_n8n_container(prior, environment)
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
                env=_service_environment("n8n", environment),
            )
            if completed.returncode != 0:
                raise RuntimeError("n8n process stop failed")
            deadline = time.monotonic() + 5.0
            while container_id is not None and time.monotonic() < deadline:
                if _observed_n8n_container_identity(environment) is None:
                    break
                time.sleep(0.1)
            if container_id is not None and _observed_n8n_container_identity(environment) is not None:
                subprocess.run(
                    ("docker", "kill", container_id), check=False, timeout=10,
                    env=_service_environment("n8n", environment),
                )
                if _observed_n8n_container_identity(environment) is not None:
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
            _require_same_n8n_container(current, environment)
        else:
            _require_same_process(current)
        _wait_authenticated_health(service, environment=environment, timeout_seconds=5.0)
        return current
    identity = str(uuid4())
    state = {
        "schema": "captain.production-process-state.v1",
        "service": service,
        "status": "starting",
        "operation": action,
        "process_id": None,
        "process_identity": identity,
        "identity_kind": "sentinel" if sentinel else (
            "docker_container" if service == "n8n" else "os_process"
        ),
        "identity_value": identity if sentinel else None,
        "argv": list(argv),
    }
    _write_state(state_file, state)
    if sentinel:
        process_id = int(identity.replace("-", "")[:8], 16) or 1
    elif service == "n8n":
        completed = subprocess.run(
            argv, cwd=repository_root, check=False, timeout=30,
            env=_service_environment("n8n", environment),
        )
        if completed.returncode != 0:
            raise RuntimeError("n8n process start failed")
        observed_container = _observed_n8n_container_identity(environment)
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
        state["process_id"] = process_id
        _write_state(state_file, state)
        observed_identity = _observed_process_identity(process_id)
        if observed_identity is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
            raise RuntimeError("started process identity is unavailable")
    state["process_id"] = process_id
    if not sentinel and service != "n8n":
        state["executable"], state["os_start_identity"] = observed_identity
        state["identity_value"] = f"{observed_identity[0]}:{observed_identity[1]}"
    elif not sentinel:
        state["identity_value"] = f"{observed_container[0]}:{observed_container[1]}"
    else:
        state["identity_value"] = identity
    state["status"] = "running" if sentinel else "starting"
    _write_state(state_file, state)
    if not sentinel:
        try:
            _wait_authenticated_health(service, environment=environment)
            if service == "n8n":
                _require_same_n8n_container(state, environment)
            else:
                _require_same_process(state)
        except Exception:
            gone = False
            try:
                if service == "n8n":
                    container_id = _require_same_n8n_container(state, environment)
                    subprocess.run(
                        ("docker", "kill", container_id), check=False, timeout=10,
                        env=_service_environment("n8n", environment),
                    )
                    gone = _observed_n8n_container_identity(environment) is None
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
    state["status"] = "running"
    _write_state(state_file, state)
    return state


def operate(
    service: str,
    action: str,
    *,
    environment: Mapping[str, str],
    repository_root: Path,
) -> dict[str, object]:
    _preflight_operation(
        service, action, environment=environment, repository_root=repository_root,
    )
    state_file = _state_root(environment, repository_root) / f"{service}.json"
    with _exclusive_state_lock(state_file):
        return _operate_locked(
            service, action, environment=environment, repository_root=repository_root,
        )


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

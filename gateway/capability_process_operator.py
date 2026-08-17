"""Semantic start/stop/restart operator for the six production processes."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


_SERVICES = ("gateway", "runtime", "provider_proxy", "benchmark", "minibook", "n8n")
_ACTIONS = ("start", "stop", "restart")


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


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise ValueError(f"required process setting is missing: {name}")
    return value


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
        if not sentinel and service == "n8n":
            completed = subprocess.run(
                service_argv(
                    service,
                    environment=environment,
                    repository_root=repository_root,
                    action="stop",
                ),
                cwd=repository_root,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("n8n process stop failed")
        if prior is not None:
            pid = int(prior.get("process_id", 0))
            if not sentinel and service != "n8n" and _pid_is_live(pid):
                os.kill(pid, signal.SIGTERM)
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
            "process_id": 0,
            "process_identity": "absent",
        }

    argv = service_argv(
        service, environment=environment, repository_root=repository_root, action="start"
    )
    current = _read_state(state_file)
    if action == "start" and current is not None and current.get("status") == "running":
        pid = int(current.get("process_id", 0))
        if sentinel or _pid_is_live(pid):
            return current
    identity = str(uuid4())
    if sentinel:
        process_id = int(identity.replace("-", "")[:8], 16) or 1
    elif service == "n8n":
        completed = subprocess.run(argv, cwd=repository_root, check=False)
        if completed.returncode != 0:
            raise RuntimeError("n8n process start failed")
        process_id = 0
    else:
        logs = _state_root(environment, repository_root) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        output = (logs / f"{service}.log").open("ab")
        process = subprocess.Popen(
            argv,
            cwd=repository_root,
            env=dict(environment),
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
    state = {
        "schema": "captain.production-process-state.v1",
        "service": service,
        "status": "running",
        "operation": action,
        "process_id": process_id,
        "process_identity": identity,
        "argv": list(argv),
    }
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

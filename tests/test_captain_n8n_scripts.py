import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_fake_docker(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True)
    (bin_dir / "docker.cmd").write_text(
        """@echo off
echo %*>>"%FAKE_DOCKER_LOG%"
if "%1"=="version" (
  echo 27.0.0
  exit /b 0
)
if "%1"=="ps" (
  echo %* | %SystemRoot%\\System32\\findstr.exe /C:"-aq" >nul
  if not errorlevel 1 (
    echo id-n8n
    echo id-postgres
    exit /b 0
  )
  echo id-n8n
  exit /b 0
)
if "%1"=="inspect" (
  echo %* | %SystemRoot%\\System32\\findstr.exe /C:"id-postgres" >nul
  if not errorlevel 1 (
    echo postgres
    exit /b 0
  )
  echo %* | %SystemRoot%\\System32\\findstr.exe /C:"id-n8n" >nul
  if not errorlevel 1 (
    echo n8n
    exit /b 0
  )
)
if "%1"=="compose" exit /b 0
exit /b 1
""",
        encoding="utf-8",
    )


@pytest.fixture
def script_sandbox(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "captain"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "captain-n8n.ps1", scripts / "captain-n8n.ps1")
    (root / "docker-compose.captain-n8n.yml").write_text(
        "name: captain-n8n-builder\nservices: {}\n", encoding="utf-8"
    )

    owner_secret = "Owner" + "123" + "Fixture"
    env_file = root / ".env.captain-n8n"
    docker_log = tmp_path / "docker.log"
    bin_dir = tmp_path / "bin"
    _write_fake_docker(bin_dir)

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["FAKE_DOCKER_LOG"] = str(docker_log)
    return {
        "root": root,
        "script": scripts / "captain-n8n.ps1",
        "env_file": env_file,
        "docker_log": docker_log,
        "environment": environment,
        "owner_secret": owner_secret,
    }


def _write_environment(
    sandbox: dict[str, Any], port: int, *, api_key: str | None = None
) -> None:
    lines = [
        f"CAPTAIN_N8N_PORT={port}",
        "CAPTAIN_N8N_ENCRYPTION_KEY=" + "encryption-" + "fixture",
        "CAPTAIN_N8N_POSTGRES_PASSWORD=" + "database-" + "fixture",
        "CAPTAIN_N8N_POSTGRES_USER=captain_n8n",
        "CAPTAIN_N8N_POSTGRES_DB=captain_n8n",
        f"CAPTAIN_N8N_OWNER_PASSWORD={sandbox['owner_secret']}",
    ]
    if api_key is not None:
        lines.append(f"CAPTAIN_N8N_API_KEY={api_key}")
    sandbox["env_file"].write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_action(sandbox: dict[str, Any], action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(sandbox["script"]),
            "-Action",
            action,
        ],
        cwd=sandbox["root"],
        env=sandbox["environment"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


Responder = Callable[[str, str, dict[str, Any] | None], tuple[int, object]]


class _MockN8nServer:
    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length else b""
                body = json.loads(raw_body) if raw_body else None
                path = self.path.split("?", 1)[0]
                outer.requests.append((self.command, path, body))
                status, payload = outer.responder(self.command, path, body)
                encoded = (
                    payload.encode("utf-8")
                    if isinstance(payload, str)
                    else json.dumps(payload).encode("utf-8")
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = _respond
            do_POST = _respond

            def log_message(self, _format: str, *args: object) -> None:
                del args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "_MockN8nServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_lifecycle_script_scopes_every_compose_call() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")

    assert "-p captain-n8n-builder" in source
    assert "--env-file $EnvFile" in source
    assert "-f $ComposeFile" in source
    assert "down -v" not in source.lower()
    assert "docker volume" not in source.lower()
    assert "vibemind-n8n" not in source.lower()
    assert "captain@local.test" in source
    assert "ConvertTo-SecureString" in source


def test_lifecycle_script_uses_secure_secrets_and_safe_operations() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")

    assert "RandomNumberGenerator]::Create()" in source
    assert "System.Net.Sockets.TcpListener" in source
    assert "up -d --wait" in source
    assert " stop" in source
    assert "com.docker.compose.project=captain-n8n-builder" in source
    assert "/rest/owner/setup" in source
    assert "/rest/login" in source
    assert "/rest/api-keys/scopes" in source
    assert "/rest/api-keys" in source
    assert "rawApiKey" in source
    assert "CAPTAIN_N8N_API_KEY" in source
    assert "psql" not in source.lower()
    assert "/var/lib/postgresql" not in source.lower()
    assert "$IsWindows" not in source


def test_bootstrap_never_echoes_secret_values() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")

    assert "Write-Host $ApiKey" not in source
    assert "Write-Output $OwnerPassword" not in source
    assert "Write-Host $OwnerPassword" not in source
    assert "Write-Output $ApiKey" not in source


def test_verifier_is_project_scoped_and_uses_authenticated_harmless_read() -> None:
    source = (ROOT / "scripts" / "verify_captain_n8n.ps1").read_text(
        encoding="utf-8"
    )

    assert "com.docker.compose.project=captain-n8n-builder" in source
    assert "/healthz" in source
    assert "/api/v1/workflows" in source
    assert "X-N8N-API-KEY" in source
    assert "vibemind-n8n" not in source.lower()
    assert "Write-Host $ApiKey" not in source
    assert "Write-Output $ApiKey" not in source


def test_readme_documents_only_captain_builder_lifecycle() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for action in ("init", "start", "bootstrap", "status", "stop"):
        assert f"scripts/captain-n8n.ps1 -Action {action}" in readme
    assert "captain@local.test" in readme
    assert "VibeMind remains untouched" in readme


def test_start_and_stop_invoke_only_fully_scoped_compose_commands(
    script_sandbox: dict[str, Any],
) -> None:
    _write_environment(script_sandbox, _free_port())

    started = _run_action(script_sandbox, "start")
    stopped = _run_action(script_sandbox, "stop")

    assert started.returncode == 0, started.stdout + started.stderr
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    calls = script_sandbox["docker_log"].read_text(encoding="utf-8").splitlines()
    compose_calls = [call for call in calls if call.startswith("compose ")]
    expected_prefix = (
        "compose -p captain-n8n-builder "
        f"--env-file {script_sandbox['env_file']} "
        f"-f {script_sandbox['root'] / 'docker-compose.captain-n8n.yml'} "
    )
    assert compose_calls
    assert all(call.startswith(expected_prefix) for call in compose_calls)
    assert any(call.endswith("up -d --wait") for call in compose_calls)
    assert any(call.endswith("stop") for call in compose_calls)
    assert not any(" down" in call or " -v" in call for call in compose_calls)


def test_bootstrap_rejects_malformed_owner_response_without_emitting_secret(
    script_sandbox: dict[str, Any],
) -> None:
    echoed_secret = script_sandbox["owner_secret"]

    def responder(method: str, path: str, _body: dict[str, Any] | None) -> tuple[int, object]:
        if method == "GET" and path == "/healthz":
            return 200, "ok"
        if method == "POST" and path == "/rest/login":
            return 401, {"message": "not configured"}
        if method == "POST" and path == "/rest/owner/setup":
            return 200, {"data": {"unexpected": echoed_secret}}
        raise AssertionError(f"Unexpected request: {method} {path}")

    with _MockN8nServer(responder) as server:
        _write_environment(script_sandbox, server.port)
        result = _run_action(script_sandbox, "bootstrap")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "unsupported owner schema" in output
    assert echoed_secret not in output
    assert [(method, path) for method, path, _body in server.requests] == [
        ("GET", "/healthz"),
        ("POST", "/rest/login"),
        ("POST", "/rest/owner/setup"),
    ]


def test_bootstrap_rejects_malformed_api_key_response_without_emitting_secret(
    script_sandbox: dict[str, Any],
) -> None:
    echoed_secret = "response-" + "api-key-" + "fixture"

    def responder(method: str, path: str, _body: dict[str, Any] | None) -> tuple[int, object]:
        if method == "GET" and path == "/healthz":
            return 200, "ok"
        if method == "POST" and path == "/rest/login":
            return 200, {"data": {"email": "captain@local.test"}}
        if method == "GET" and path == "/rest/api-keys":
            return 200, {"data": {"items": []}}
        if method == "GET" and path == "/rest/api-keys/scopes":
            return 200, {"data": ["workflow:read"]}
        if method == "POST" and path == "/rest/api-keys":
            return 200, {"data": {"diagnostic": echoed_secret}}
        raise AssertionError(f"Unexpected request: {method} {path}")

    with _MockN8nServer(responder) as server:
        _write_environment(script_sandbox, server.port)
        result = _run_action(script_sandbox, "bootstrap")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "omitted rawApiKey" in output
    assert echoed_secret not in output
    assert script_sandbox["owner_secret"] not in output
    assert ("POST", "/rest/api-keys") in [
        (method, path) for method, path, _body in server.requests
    ]


def test_invalid_stored_api_key_is_not_emitted(
    script_sandbox: dict[str, Any],
) -> None:
    api_key = "stored-" + "api-key-" + "fixture"

    def responder(method: str, path: str, _body: dict[str, Any] | None) -> tuple[int, object]:
        if method == "GET" and path == "/healthz":
            return 200, "ok"
        if method == "GET" and path == "/api/v1/workflows":
            return 401, {"message": api_key}
        raise AssertionError(f"Unexpected request: {method} {path}")

    with _MockN8nServer(responder) as server:
        _write_environment(script_sandbox, server.port, api_key=api_key)
        result = _run_action(script_sandbox, "bootstrap")

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "workflows endpoint returned HTTP 401" in output
    assert api_key not in output

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import httpx
import pytest

from agenten.delivery.codex_runs import (
    ActiveCodexSessionRecoveryRequired,
    CancellationExecutionError,
    CancellationPersistenceRequired,
    CapabilityGrantRevocationMonitor,
    CodexCancellationCoordinator,
    CodexCancellationResult,
    CodexOutcome,
    CodexRunRecord,
    GatewayCodexRunRepository,
    SessionBoundCodexCanceller,
)
from agenten.agent_runtime.contracts import CapabilityGrantRevocation
from agenten.delivery.gateway_client import GatewayDeliveryClient
from agenten.execution.codex_events import CodexParseWarning, CodexProcessEvent
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment
from agenten.execution.codex_supervisor import (
    CodexJournalPersistenceError,
    CodexOutputEvidenceError,
    CodexRunRequest,
    CodexRunResult,
    CodexRunTerminalStatus,
    CodexSupervisor,
    PowerShellCodexRunner,
)
from agenten.execution.process import PackageExecutionStatus


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
FUTURE_DEADLINE = datetime(2100, 1, 1, tzinfo=timezone.utc)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _durable_codex_result(
    *,
    journal_path: Path,
    exit_code: int,
    terminal_status: CodexRunTerminalStatus,
    artifact_references: tuple[str, ...],
    jsonl_lines: tuple[str, ...],
) -> CodexRunResult:
    journal_bytes = b"".join(f"{line}\n".encode() for line in jsonl_lines)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("wb") as journal:
        journal.write(journal_bytes)
        journal.flush()
        os.fsync(journal.fileno())
    return CodexRunResult(
        exit_code=exit_code,
        terminal_status=terminal_status,
        process_cleanup_status="not_required",
        journal_path=journal_path.resolve(),
        journal_sha256=hashlib.sha256(journal_bytes).hexdigest(),
        artifact_references=artifact_references,
        jsonl_lines=jsonl_lines,
    )


class RevocationReader:
    def __init__(self) -> None:
        self.revocation: CapabilityGrantRevocation | None = None

    async def get_grant_revocation(
        self, command_id: object
    ) -> CapabilityGrantRevocation | None:
        del command_id
        return self.revocation


class GatewayHistory:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.requests: list[httpx.Request] = []
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=self.events, request=request)
        event = json.loads(request.content)
        existing = next(
            (item for item in self.events if item["event_id"] == event["event_id"]),
            None,
        )
        if existing is None:
            self.events.append(event)
            status = 201
            replayed = False
        else:
            assert existing == event
            status = 200
            replayed = True
        return httpx.Response(
            status,
            json={"event": event, "replayed": replayed},
            request=request,
        )


class AllowingPolicy:
    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun:
        return AuthorizedCodexRun(
            workspace=request.workspace,
            command=request.command,
            environment=FrozenEnvironment({"PATH": "safe"}),
        )


class ForbiddenLegacyWriter:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"legacy writer called: {name}")


def request(tmp_path: Path, *, prompt: str = "build safely") -> CodexRunRequest:
    return CodexRunRequest(
        project_id="project-1",
        run_id="run-1",
        trace_id="trace-1",
        batch_id="batch-1",
        worker_id="worker-1",
        claim_id="claim-1",
        fencing_token=7,
        session_id="session-1",
        claim_token="claim-secret",
        iteration=1,
        command=("codex", "exec", "--json", prompt),
        workspace=tmp_path,
        project_root=tmp_path,
    )


def repository(
    history: GatewayHistory,
    http: httpx.AsyncClient,
) -> GatewayCodexRunRepository:
    return GatewayCodexRunRepository(
        client=GatewayDeliveryClient("http://gateway", "worker-secret", http),
        project_id="project-1",
        run_id="run-1",
        actor="worker-1",
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_supervisor_persists_started_session_before_runner(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)

        class AssertingRunner:
            async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
                assert history.events[0]["event_type"] == "codex_session_started"
                return _durable_codex_result(
                    journal_path=tmp_path / "runner.jsonl",
                    exit_code=0,
                    terminal_status="succeeded",
                    artifact_references=("artifact://sealed/1",),
                    jsonl_lines=(
                        '{"type":"thread.started","thread_id":"session-1"}',
                    ),
                )

        result = await CodexSupervisor(
            runner=AssertingRunner(),
            gateway=ForbiddenLegacyWriter(),
            policy=AllowingPolicy(),
            repository=runs,
        ).run(request(tmp_path))

    assert result.status is PackageExecutionStatus.SUCCEEDED
    journal_path = tmp_path / "runner.jsonl"
    assert journal_path.is_file()
    assert journal_path.read_bytes() == (
        b'{"type":"thread.started","thread_id":"session-1"}\n'
    )
    assert [event["event_type"] for event in history.events] == [
        "codex_session_started",
        "codex_session_event",
        "codex_session_finished",
    ]


@pytest.mark.asyncio
async def test_active_sessions_are_recovered_and_lost_process_is_durable_once(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        first = repository(history, http)
        started = await first.start(request(tmp_path))

        restarted = repository(history, http)
        assert await restarted.active(worker_id="worker-1") == (started,)

        reconciled = await restarted.reconcile(
            worker_id="worker-1", live_process_ids=frozenset()
        )
        assert reconciled[0].outcome.classification == "lost_process"
        assert reconciled[0].outcome.behavioral_repair_increment == 0
        assert await restarted.reconcile(
            worker_id="worker-1", live_process_ids=frozenset()
        ) == ()
        assert await restarted.active(worker_id="worker-1") == ()

    assert [
        event["event_type"] for event in history.events
    ].count("codex_session_finished") == 1


@pytest.mark.asyncio
async def test_concurrent_restart_claims_admit_only_one_codex_session_owner(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        first = repository(history, http)
        second = repository(history, http)
        results = await asyncio.gather(
            first.start(request(tmp_path)),
            second.start(request(tmp_path)),
            return_exceptions=True,
        )

    assert sum(isinstance(item, CodexRunRecord) for item in results) == 1
    assert sum(
        isinstance(item, ActiveCodexSessionRecoveryRequired) for item in results
    ) == 1
    assert [event["event_type"] for event in history.events] == [
        "codex_session_started"
    ]


@pytest.mark.asyncio
async def test_cancellation_and_infrastructure_outcomes_never_consume_repair(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)
        await runs.start(request(tmp_path))
        await runs.persist_cancellation(
            CodexCancellationResult(
                session_id="session-1",
                outcome="cancelled",
                cancellation_reason="operator",
            )
        )

    terminal = history.events[-1]["payload"]
    assert terminal["outcome"] == "cancelled"
    assert terminal["cancellation_reason"] == "operator"
    assert terminal["behavioral_repair_increment"] == 0


@pytest.mark.asyncio
async def test_replay_and_sanitized_event_payloads_are_idempotent_and_secret_free(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    raw_prompt = "use TOP_SECRET_PROMPT"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)
        first = await runs.start(request(tmp_path, prompt=raw_prompt))
        with pytest.raises(ActiveCodexSessionRecoveryRequired, match="requires recovery"):
            await runs.start(request(tmp_path, prompt=raw_prompt))
        event = CodexProcessEvent(lifecycle="failed", source_sequence=0)
        warning = CodexParseWarning(
            source_sequence=1,
            warning_type="malformed_json",
            line_sha256="a" * 64,
        )
        await runs.append(event)
        await runs.append(event)
        await runs.append(warning)
        await runs.append(warning)

    serialized = json.dumps(history.events)
    assert len(history.events) == 3
    for forbidden in (
        raw_prompt,
        "TOP_SECRET_PROMPT",
        "claim-secret",
        "worker-secret",
        str(tmp_path),
        "raw error text",
    ):
        assert forbidden not in serialized
    assert history.events[1]["event_type"] == "codex_session_event"
    assert history.events[2]["event_type"] == "codex_session_warning"


@pytest.mark.asyncio
async def test_nonzero_process_exit_is_infrastructure_not_behavioral_repair(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)

        class FailedRunner:
            async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
                return _durable_codex_result(
                    journal_path=tmp_path / "runner.jsonl",
                    exit_code=23,
                    terminal_status="failed",
                    artifact_references=(),
                    jsonl_lines=(),
                )

        result = await CodexSupervisor(
            runner=FailedRunner(),
            gateway=ForbiddenLegacyWriter(),
            policy=AllowingPolicy(),
            repository=runs,
        ).run(request(tmp_path))

    assert result.status is PackageExecutionStatus.FAILED
    terminal = history.events[-1]["payload"]
    assert terminal["outcome"] == "infrastructure_failure"
    assert terminal["exit_code"] == 23
    assert terminal["behavioral_repair_increment"] == 0


def _pwsh() -> str:
    executable = shutil.which("pwsh")
    assert executable is not None
    return executable


def _process_identity(pid: int) -> dict[str, str]:
    completed = subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-Command",
            (
                f"$p = Get-Process -Id {pid}; "
                "[pscustomobject]@{"
                "started_at_utc=$p.StartTime.ToUniversalTime().ToString('O');"
                "start_time_utc_ticks=$p.StartTime.ToUniversalTime().Ticks;"
                "executable=$p.Path"
                "} | ConvertTo-Json -Compress"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _compile_streaming_fixture(
    tmp_path: Path,
    *,
    jsonl_line: str | None,
    sleep_milliseconds: int = 60_000,
    exit_code: int = 0,
) -> Path:
    executable = tmp_path / f"streaming-fixture-{uuid4().hex}.exe"
    write_line = ""
    if jsonl_line is not None:
        escaped = jsonl_line.replace('"', '""')
        write_line = (
            f'Console.Out.WriteLine(@"{escaped}");'
            "Console.Out.Flush();"
            'Console.Error.WriteLine("private diagnostic");'
            "Console.Error.Flush();"
        )
    source_path = executable.with_suffix(".cs")
    source_path.write_text(
        (
            "using System;using System.Threading;"
            "public static class Program {"
            "public static int Main(string[] args) {"
            f"{write_line}Thread.Sleep({sleep_milliseconds});return {exit_code};"
            "}}"
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
            "/nologo",
            f"/out:{executable}",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return executable


def _compile_large_stderr_fixture(tmp_path: Path) -> Path:
    executable = tmp_path / f"large-stderr-fixture-{uuid4().hex}.exe"
    source_path = executable.with_suffix(".cs")
    source_path.write_text(
        (
            "using System;using System.IO;"
            "public static class Program {"
            "public static int Main(string[] args) {"
            "byte[] chunk = new byte[65536];"
            "Stream stderr = Console.OpenStandardError();"
            "for (int i = 0; i < 1024; i++) { stderr.Write(chunk, 0, chunk.Length); }"
            "stderr.Flush();"
            'Console.Out.WriteLine("{\\\"type\\\":\\\"turn.started\\\"}");'
            "Console.Out.Flush();return 0;"
            "}}"
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
            "/nologo",
            f"/out:{executable}",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return executable


def _compile_marker_fixture(tmp_path: Path, marker_path: Path) -> Path:
    executable = tmp_path / f"marker-fixture-{uuid4().hex}.exe"
    escaped_marker = marker_path.as_posix().replace('"', '""')
    source_path = executable.with_suffix(".cs")
    source_path.write_text(
        (
            "using System.IO;"
            "public static class Program {"
            "public static int Main(string[] args) {"
            f'File.WriteAllText(@"{escaped_marker}", "started");return 0;'
            "}}"
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
            "/nologo",
            f"/out:{executable}",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return executable


@pytest.mark.asyncio
async def test_powershell_runner_expired_deadline_never_starts_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper_starts: list[object] = []

    async def forbidden_wrapper_start(*_args: object, **_kwargs: object) -> object:
        wrapper_starts.append(object())
        raise AssertionError("expired authority must not start PowerShell")

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        forbidden_wrapper_start,
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    journal_path = tmp_path / "private" / "expired.jsonl"
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-expired-1",
        state_path=tmp_path / "expired-process.json",
        journal_path=journal_path,
        artifact_references=("artifact://must-not-escape",),
        codex_home=codex_home,
        deadline_at=NOW,
        clock=lambda: NOW,
    )

    result = await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "must not run"),
            environment=FrozenEnvironment({"PATH": "safe"}),
        )
    )

    assert wrapper_starts == []
    assert result.exit_code == 124
    assert result.terminal_status == "timed_out"
    assert result.process_cleanup_status == "not_required"
    assert result.artifact_references == ()
    assert result.journal_path == journal_path.resolve()
    assert journal_path.read_bytes() == b""


@pytest.mark.parametrize(
    "deadline_at",
    (
        NOW.replace(tzinfo=None),
        NOW.astimezone(timezone(timedelta(hours=1))),
    ),
    ids=("naive", "non-utc-offset"),
)
def test_powershell_runner_rejects_non_utc_deadline(
    tmp_path: Path,
    deadline_at: datetime,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    with pytest.raises(ValueError, match="UTC timestamp"):
        PowerShellCodexRunner(
            pwsh_path=Path(_pwsh()),
            script_path=Path("scripts/codex-session.ps1").resolve(),
            codex_path=Path(r"C:\Windows\System32\timeout.exe"),
            session_id="runner-invalid-deadline-1",
            state_path=tmp_path / "invalid-process.json",
            journal_path=tmp_path / "invalid.jsonl",
            artifact_references=(),
            codex_home=codex_home,
            deadline_at=deadline_at,
        )


@pytest.mark.asyncio
async def test_powershell_runner_budget_starts_when_wrapper_launch_begins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def create_process(*args: object, **_kwargs: object) -> Process:
        captured["args"] = args
        return Process()

    async def capture_wait_for(awaitable, *, timeout: float):
        captured["timeout"] = timeout
        return await awaitable

    monotonic_values = iter((100.0, 103.0, 104.0))
    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.wait_for",
        capture_wait_for,
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-budget-1",
        state_path=tmp_path / "budget-process.json",
        journal_path=tmp_path / "budget.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        timeout_seconds=5,
        deadline_at=NOW + timedelta(seconds=10),
        clock=lambda: NOW,
        monotonic=lambda: next(monotonic_values),
    )

    await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless test"),
            environment=FrozenEnvironment({"PATH": "safe"}),
        )
    )

    assert captured["timeout"] == 1.0
    args = captured["args"]
    assert isinstance(args, tuple)
    deadline_index = args.index("-DeadlineAt")
    assert args[deadline_index + 1] == (NOW + timedelta(seconds=10)).isoformat()


def test_powershell_launcher_expired_deadline_never_starts_codex_child(
    tmp_path: Path,
) -> None:
    marker_path = tmp_path / "child-started.txt"
    fixture = _compile_marker_fixture(tmp_path, marker_path)
    state_path = tmp_path / "expired-child-process.json"

    completed = subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-File",
            str(Path("scripts/codex-session.ps1").resolve()),
            "-Workspace",
            str(tmp_path),
            "-Prompt",
            "must not run",
            "-CodexPath",
            str(fixture),
            "-SessionId",
            "expired-child-1",
            "-StatePath",
            str(state_path),
            "-DeadlineAt",
            (NOW - timedelta(seconds=1)).isoformat(),
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 124
    assert not marker_path.exists()
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_powershell_launcher_streams_complete_line_before_child_exit(
    tmp_path: Path,
) -> None:
    line = '{"type":"thread.started","thread_id":"fixture-thread"}'
    fixture = _compile_streaming_fixture(tmp_path, jsonl_line=line)
    state_path = tmp_path / "streaming-process.json"
    process = await asyncio.create_subprocess_exec(
        _pwsh(),
        "-NoProfile",
        "-File",
        str(Path("scripts/codex-session.ps1").resolve()),
        "-Workspace",
        str(tmp_path),
        "-Prompt",
        "harmless streaming test",
        "-CodexPath",
        str(fixture),
        "-SessionId",
        "runner-stream-1",
        "-StatePath",
        str(state_path),
        "-DeadlineAt",
        FUTURE_DEADLINE.isoformat(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    identity: dict[str, object] | None = None
    try:
        observed = await asyncio.wait_for(process.stdout.readline(), timeout=5)
        assert observed.endswith(b"\n")
        assert observed.rstrip(b"\r\n") == line.encode()
        assert process.returncode is None
        identity = json.loads(state_path.read_text(encoding="utf-8"))
    finally:
        if identity is None and state_path.is_file():
            identity = json.loads(state_path.read_text(encoding="utf-8"))
        if identity is not None:
            subprocess.run(
                ["taskkill.exe", "/PID", str(identity["pid"]), "/T", "/F"],
                capture_output=True,
            )
        if process.returncode is None:
            process.kill()
        await process.wait()


@pytest.mark.asyncio
async def test_powershell_launcher_drains_large_stderr_without_retaining_it(
    tmp_path: Path,
) -> None:
    fixture = _compile_large_stderr_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=fixture,
        session_id="large-stderr-1",
        state_path=tmp_path / "large-stderr-process.json",
        journal_path=tmp_path / "private" / "large-stderr.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
        timeout_seconds=15,
    )

    result = await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless stderr test"),
            environment=FrozenEnvironment({"PATH": os.environ["PATH"]}),
        )
    )

    launcher = Path("scripts/codex-session.ps1").read_text(encoding="utf-8")
    assert "ReadToEndAsync" not in launcher
    assert "StandardError.BaseStream.CopyToAsync" in launcher
    assert result.terminal_status == "succeeded"
    assert result.jsonl_lines == ('{"type":"turn.started"}',)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("line", "expected_lines"),
    [
        (None, ()),
        ('{"type":"turn.started"}', ('{"type":"turn.started"}',)),
    ],
    ids=("zero-jsonl", "partial-jsonl"),
)
async def test_powershell_runner_timeout_retains_durable_journal(
    tmp_path: Path,
    line: str | None,
    expected_lines: tuple[str, ...],
) -> None:
    fixture = _compile_streaming_fixture(tmp_path, jsonl_line=line)
    journal_path = tmp_path / "private" / "session.jsonl"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=fixture,
        session_id=f"runner-timeout-{uuid4().hex}",
        state_path=tmp_path / "timed-out-process.json",
        journal_path=journal_path,
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
        timeout_seconds=1.0,
    )

    result = await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless timeout test"),
            environment=FrozenEnvironment({"PATH": os.environ["PATH"]}),
        )
    )

    journal_bytes = journal_path.read_bytes()
    assert result.exit_code == 124
    assert result.terminal_status == "timed_out"
    assert result.process_cleanup_status == "verified_cancelled"
    assert result.journal_path == journal_path.resolve()
    assert result.journal_sha256 == hashlib.sha256(journal_bytes).hexdigest()
    assert result.jsonl_lines == expected_lines
    assert journal_bytes == b"".join(
        f"{jsonl_line}\n".encode() for jsonl_line in expected_lines
    )
    assert b"private diagnostic" not in journal_bytes


class _UnverifiedTimeoutProcess:
    def __init__(self, line: str | None) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        if line is not None:
            self.stdout.feed_data(f"{line}\n".encode())
        self.stderr = asyncio.StreamReader()
        self._wait_calls = 0

    async def wait(self) -> int:
        self._wait_calls += 1
        if self._wait_calls == 1:
            await asyncio.Future()
        self.returncode = 1
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()


class _CompletedStreamingProcess:
    def __init__(self, line: str, *, exit_code: int = 0) -> None:
        self.returncode: int | None = None
        self._exit_code = exit_code
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(f"{line}\n".encode())
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self.returncode


class _PendingJsonlProcess:
    def __init__(self, lines: tuple[str, ...]) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(f"{line}\n".encode("utf-8"))
        self.stderr = asyncio.StreamReader()
        self._wait_calls = 0

    async def wait(self) -> int:
        self._wait_calls += 1
        if self._wait_calls == 1:
            await asyncio.Future()
        self.returncode = 1
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lines", "expected_journal", "expected_event_types"),
    (
        (("not-json",), b"", ()),
        (("[]",), b"", ()),
        (("42",), b"", ()),
        (
            ('{"type":"SENSITIVE_MARKER"}', "null"),
            b'{"type":"SENSITIVE_MARKER"}\n',
            ("unknown",),
        ),
    ),
    ids=("malformed-first", "array-first", "scalar-first", "malformed-after-valid"),
)
async def test_powershell_runner_rejects_every_non_object_jsonl_record_with_partial_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: tuple[str, ...],
    expected_journal: bytes,
    expected_event_types: tuple[str, ...],
) -> None:
    process = _PendingJsonlProcess(lines)
    cancellation_calls = 0

    async def create_process(*args: object, **kwargs: object) -> object:
        return process

    async def verified_cancel() -> bool:
        nonlocal cancellation_calls
        cancellation_calls += 1
        return True

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    journal_path = tmp_path / "private" / "invalid.jsonl"
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-invalid-jsonl",
        state_path=tmp_path / "process-state.json",
        journal_path=journal_path,
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
        timeout_seconds=0.1,
    )
    monkeypatch.setattr(
        runner,
        "_cancel_evidence_failure_process",
        verified_cancel,
    )

    with pytest.raises(CodexOutputEvidenceError) as caught:
        await runner.run(
            AuthorizedCodexRun(
                workspace=tmp_path,
                command=("codex", "exec", "--json", "harmless invalid output"),
                environment=FrozenEnvironment({"PATH": "safe"}),
            )
        )

    error = caught.value
    assert error.failure_kind == "invalid_json_object"
    assert error.process_cleanup_status == "verified_cancelled"
    assert error.journal_path == journal_path.resolve()
    assert error.journal_byte_count == len(expected_journal)
    assert error.journal_sha256 == hashlib.sha256(expected_journal).hexdigest()
    assert error.event_count == len(expected_journal.splitlines())
    assert error.event_types == expected_event_types
    assert journal_path.read_bytes() == expected_journal
    assert cancellation_calls == 1


@pytest.mark.asyncio
async def test_powershell_runner_classifies_completed_exit_130_as_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _CompletedStreamingProcess('{"type":"turn.started"}', exit_code=130)

    async def create_process(*args: object, **kwargs: object) -> object:
        return process

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-exit-130",
        state_path=tmp_path / "process-state.json",
        journal_path=tmp_path / "private" / "cancelled.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
    )

    result = await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless cancellation"),
            environment=FrozenEnvironment({"PATH": "safe"}),
        )
    )

    assert result.exit_code == 130
    assert result.terminal_status == "cancelled"
    assert result.process_cleanup_status == "not_required"


@pytest.mark.asyncio
async def test_real_powershell_runner_preserves_exit_130_cancellation_contract(
    tmp_path: Path,
) -> None:
    fixture = _compile_streaming_fixture(
        tmp_path,
        jsonl_line='{"type":"turn.started"}',
        sleep_milliseconds=0,
        exit_code=130,
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=fixture,
        session_id="runner-real-exit-130",
        state_path=tmp_path / "process-state.json",
        journal_path=tmp_path / "private" / "cancelled.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
    )

    result = await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless cancellation"),
            environment=FrozenEnvironment({"PATH": os.environ["PATH"]}),
        )
    )

    assert result.exit_code == 130
    assert result.terminal_status == "cancelled"
    assert result.process_cleanup_status == "not_required"


@pytest.mark.asyncio
async def test_powershell_runner_fails_closed_when_journal_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _CompletedStreamingProcess('{"type":"turn.started"}')
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-fsync-failure",
        state_path=tmp_path / "process-state.json",
        journal_path=tmp_path / "private" / "fsync-failure.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
        timeout_seconds=1,
    )

    async def create_process(*args: object, **kwargs: object) -> object:
        return process

    def fsync_fails(file_descriptor: int) -> None:
        del file_descriptor
        raise OSError("private journal device failure")

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.os.fsync",
        fsync_fails,
    )

    with pytest.raises(
        CodexJournalPersistenceError,
        match="Codex JSONL journal persistence failed",
    ) as error:
        await runner.run(
            AuthorizedCodexRun(
                workspace=tmp_path,
                command=("codex", "exec", "--json", "harmless persistence test"),
                environment=FrozenEnvironment({"PATH": "safe"}),
            )
        )
    assert "private journal device failure" not in str(error.value)


class _BrokenCleanupProcess(_UnverifiedTimeoutProcess):
    async def wait(self) -> int:
        self._wait_calls += 1
        if self._wait_calls == 1:
            await asyncio.Future()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        raise RuntimeError("private cleanup failure")


def _unverified_timeout_runner(tmp_path: Path) -> PowerShellCodexRunner:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    return PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id=f"runner-unverified-{uuid4().hex}",
        state_path=tmp_path / "missing-process-state.json",
        journal_path=tmp_path / "private" / "unverified.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
        timeout_seconds=0.001,
    )


async def _run_unverified_timeout(
    runner: PowerShellCodexRunner,
    tmp_path: Path,
) -> CodexRunResult:
    return await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless timeout test"),
            environment=FrozenEnvironment({"PATH": "safe"}),
        )
    )


@pytest.mark.asyncio
async def test_powershell_runner_timeout_with_missing_state_returns_unresolved_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    line = '{"type":"turn.started"}'
    process = _UnverifiedTimeoutProcess(line)

    async def create_process(*args: object, **kwargs: object) -> object:
        return process

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    result = await _run_unverified_timeout(
        _unverified_timeout_runner(tmp_path),
        tmp_path,
    )

    assert result.exit_code == 124
    assert result.terminal_status == "timed_out"
    assert result.process_cleanup_status == "unresolved"
    assert result.jsonl_lines == (line,)


@pytest.mark.asyncio
async def test_powershell_runner_timeout_with_failed_verified_cancellation_returns_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _UnverifiedTimeoutProcess(None)
    runner = _unverified_timeout_runner(tmp_path)
    cancellation_attempted = False

    async def create_process(*args: object, **kwargs: object) -> object:
        return process

    async def cancellation_fails() -> bool:
        nonlocal cancellation_attempted
        cancellation_attempted = True
        return False

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    monkeypatch.setattr(runner, "_cancel_timed_out_process", cancellation_fails)
    result = await _run_unverified_timeout(runner, tmp_path)

    assert cancellation_attempted is True
    assert result.exit_code == 124
    assert result.terminal_status == "timed_out"
    assert result.process_cleanup_status == "unresolved"
    assert result.jsonl_lines == ()


@pytest.mark.asyncio
async def test_powershell_runner_timeout_with_wrapper_cleanup_error_returns_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _BrokenCleanupProcess(None)

    async def create_process(*args: object, **kwargs: object) -> object:
        return process

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    result = await _run_unverified_timeout(
        _unverified_timeout_runner(tmp_path),
        tmp_path,
    )

    assert result.exit_code == 124
    assert result.terminal_status == "timed_out"
    assert result.process_cleanup_status == "unresolved"



@pytest.mark.asyncio
async def test_powershell_runner_bridges_authorized_run_to_real_launcher(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "runner-process.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-session-1",
        state_path=state_path,
        journal_path=tmp_path / "runner.jsonl",
        artifact_references=("artifact://sealed/runner-test",),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
    )
    result = await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless test"),
            environment=FrozenEnvironment({"PATH": "safe"}),
        )
    )

    assert result.exit_code != 0
    assert result.terminal_status == "failed"
    assert result.process_cleanup_status == "not_required"
    assert result.artifact_references == ("artifact://sealed/runner-test",)
    assert result.jsonl_lines == ()
    assert 'ArgumentList.Add("--sandbox")' in Path(
        "scripts/codex-session.ps1"
    ).read_text(encoding="utf-8")
    assert 'ArgumentList.Add("-a")' in Path(
        "scripts/codex-session.ps1"
    ).read_text(encoding="utf-8")
    assert 'ArgumentList.Add("never")' in Path(
        "scripts/codex-session.ps1"
    ).read_text(encoding="utf-8")
    assert 'ArgumentList.Add("--ignore-user-config")' in Path(
        "scripts/codex-session.ps1"
    ).read_text(encoding="utf-8")
    launcher = Path("scripts/codex-session.ps1").read_text(encoding="utf-8")
    assert launcher.index('ArgumentList.Add("-a")') < launcher.index(
        'ArgumentList.Add("exec")'
    )
    assert launcher.count('ArgumentList.Add("--ignore-user-config")') == 2
    assert launcher.count('ArgumentList.Add("--ignore-rules")') == 2
    assert launcher.index('ArgumentList.Add("exec")') < launcher.index(
        'ArgumentList.Add("--ignore-user-config")'
    )
    assert launcher.index('ArgumentList.Add("--ignore-user-config")') < launcher.index(
        'ArgumentList.Add("--ignore-rules")'
    )
    identity = json.loads(state_path.read_text(encoding="utf-8"))
    assert identity["session_id"] == "runner-session-1"


@pytest.mark.asyncio
async def test_capability_revocation_monitor_accepts_only_the_bound_lease() -> None:
    command_id = uuid4()
    reader = RevocationReader()
    monitor = CapabilityGrantRevocationMonitor(
        reader=reader,
        command_id=command_id,
        grant_id="grant-1",
        poll_seconds=0.001,
    )
    reader.revocation = CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id="grant-1",
        command_id=command_id,
        revoked_at=NOW,
        reason="captain_cancelled",
    )

    await monitor.wait()


@pytest.mark.asyncio
async def test_capability_revocation_monitor_fails_closed_for_other_lease() -> None:
    command_id = uuid4()
    reader = RevocationReader()
    monitor = CapabilityGrantRevocationMonitor(
        reader=reader,
        command_id=command_id,
        grant_id="grant-1",
        poll_seconds=0.001,
    )
    reader.revocation = CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id="grant-other",
        command_id=command_id,
        revoked_at=NOW,
        reason="captain_cancelled",
    )

    with pytest.raises(CancellationExecutionError, match="does not match"):
        await monitor.wait()



@pytest.mark.asyncio
async def test_powershell_runner_sets_a_scoped_codex_home_for_the_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def create_process(*args: object, **kwargs: object) -> Process:
        captured["args"] = args
        captured["environment"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    codex_home = tmp_path / "scoped-codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-scoped-home-1",
        state_path=tmp_path / "runner-process.json",
        journal_path=tmp_path / "runner.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
    )

    result = await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=("codex", "exec", "--json", "harmless test"),
            environment=FrozenEnvironment({"PATH": "safe-path"}),
        )
    )

    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["CODEX_HOME"] == str(codex_home.resolve())
    assert result.exit_code == 0
    assert result.terminal_status == "succeeded"
    assert result.process_cleanup_status == "not_required"
    assert result.journal_sha256 == EMPTY_SHA256


@pytest.mark.asyncio
async def test_powershell_runner_passes_resume_thread_and_prompt_as_distinct_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def create_process(*args: object, **kwargs: object) -> Process:
        captured["args"] = args
        return Process()

    monkeypatch.setattr(
        "agenten.execution.codex_supervisor.asyncio.create_subprocess_exec",
        create_process,
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=Path(r"C:\Windows\System32\timeout.exe"),
        session_id="runner-resume-1",
        state_path=tmp_path / "runner-process.json",
        journal_path=tmp_path / "runner.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
    )
    prompt = 'continue "exact"; $env:SHOULD_NOT_EXPAND'

    await runner.run(
        AuthorizedCodexRun(
            workspace=tmp_path,
            command=(
                "codex",
                "exec",
                "resume",
                "--json",
                "codex-thread-123",
                prompt,
            ),
            environment=FrozenEnvironment({"PATH": "safe-path"}),
        )
    )

    arguments = captured["args"]
    assert isinstance(arguments, tuple)
    assert arguments[arguments.index("-ResumeThreadId") + 1] == "codex-thread-123"
    assert arguments[arguments.index("-Prompt") + 1] == prompt
    assert "codex-thread-123" not in prompt


@pytest.mark.asyncio
async def test_powershell_runner_timeout_cancels_recorded_child_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeper = tmp_path / "harmless-sleeper.exe"
    source = (
        "using System.Threading;"
        "public static class Program {"
        "public static int Main(string[] args) { Thread.Sleep(60000); return 23; }"
        "}"
    )
    source_path = tmp_path / "harmless-sleeper.cs"
    source_path.write_text(source, encoding="utf-8")
    subprocess.run(
        [
            r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
            "/nologo",
            f"/out:{sleeper}",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    state_path = tmp_path / "timed-out-process.json"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    runner = PowerShellCodexRunner(
        pwsh_path=Path(_pwsh()),
        script_path=Path("scripts/codex-session.ps1").resolve(),
        codex_path=sleeper,
        session_id="runner-timeout-1",
        state_path=state_path,
        journal_path=tmp_path / "runner.jsonl",
        artifact_references=(),
        codex_home=codex_home,
        deadline_at=FUTURE_DEADLINE,
        timeout_seconds=2.0,
    )

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-cancel-controller")
    assert "OPENAI_API_KEY" not in runner._cancellation_environment()

    identity = None
    try:
        result = await runner.run(
            AuthorizedCodexRun(
                workspace=tmp_path,
                command=("codex", "exec", "--json", "harmless timeout test"),
                environment=FrozenEnvironment({"PATH": os.environ["PATH"]}),
            )
        )
        identity = json.loads(state_path.read_text(encoding="utf-8"))
        assert result.exit_code == 124
        assert result.artifact_references == ()
        assert result.jsonl_lines == ()
        assert identity["session_id"] == "runner-timeout-1"
        probe = subprocess.run(
            [
                _pwsh(),
                "-NoProfile",
                "-Command",
                f"Get-Process -Id {identity['pid']} -ErrorAction Stop",
            ],
            capture_output=True,
            text=True,
        )
        assert probe.returncode != 0
    finally:
        if identity is None and state_path.exists():
            identity = json.loads(state_path.read_text(encoding="utf-8"))
        if identity is not None:
            subprocess.run(
                ["taskkill.exe", "/PID", str(identity["pid"]), "/T", "/F"],
                capture_output=True,
            )


def test_powershell_7_launcher_emits_session_bound_process_identity(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "codex-process.json"
    completed = subprocess.run(
        [
            _pwsh(),
            "-NoProfile",
            "-File",
            str(Path("scripts/codex-session.ps1").resolve()),
            "-Workspace",
            str(tmp_path),
            "-Prompt",
            "harmless test",
            "-CodexPath",
            str(Path(r"C:\Windows\System32\timeout.exe")),
            "-SessionId",
            "session-pwsh-1",
            "-StatePath",
            str(state_path),
            "-DeadlineAt",
            FUTURE_DEADLINE.isoformat(),
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0  # timeout.exe is not a fake Codex success
    identity = json.loads(state_path.read_text(encoding="utf-8"))
    assert identity["session_id"] == "session-pwsh-1"
    assert identity["pid"] > 0
    assert identity["started_at_utc"].endswith("Z")
    assert identity["start_time_utc_ticks"] > 0
    assert identity["executable"].lower().endswith("timeout.exe")


def test_powershell_cancellation_validates_identity_and_kills_exact_tree(
    tmp_path: Path,
) -> None:
    child = subprocess.Popen(
        [_pwsh(), "-NoProfile", "-Command", "Start-Sleep -Seconds 60"]
    )
    try:
        state_path = tmp_path / "codex-process.json"
        process_identity = _process_identity(child.pid)
        state_path.write_text(
            json.dumps(
                {
                    "session_id": "session-cancel-1",
                    "pid": child.pid,
                    **process_identity,
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                _pwsh(),
                "-NoProfile",
                "-File",
                str(Path("scripts/codex-session.ps1").resolve()),
                "-CancelStatePath",
                str(state_path),
                "-SessionId",
                "session-cancel-1",
                "-CancellationReason",
                "operator",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        child.wait(timeout=10)
        cancellation = json.loads(completed.stdout)
        assert cancellation == {
            "session_id": "session-cancel-1",
            "outcome": "cancelled",
            "cancellation_reason": "operator",
        }
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_powershell_cancellation_rejects_pid_reuse_identity_mismatch(
    tmp_path: Path,
) -> None:
    child = subprocess.Popen(
        [_pwsh(), "-NoProfile", "-Command", "Start-Sleep -Seconds 60"]
    )
    try:
        state_path = tmp_path / "tampered-process.json"
        state_path.write_text(
            json.dumps(
                {
                    "session_id": "session-cancel-2",
                    "pid": child.pid,
                    "started_at_utc": "2000-01-01T00:00:00.0000000Z",
                    "start_time_utc_ticks": 1,
                    "executable": str(Path(_pwsh()).resolve()),
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                _pwsh(),
                "-NoProfile",
                "-File",
                str(Path("scripts/codex-session.ps1").resolve()),
                "-CancelStatePath",
                str(state_path),
                "-SessionId",
                "session-cancel-2",
                "-CancellationReason",
                "operator",
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)



def _write_process_state(path: Path, session_id: str, child: subprocess.Popen[bytes]) -> None:
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "pid": child.pid,
                **_process_identity(child.pid),
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_cancellation_coordinator_kills_and_persists_in_one_call(
    tmp_path: Path,
) -> None:
    child = subprocess.Popen(
        [_pwsh(), "-NoProfile", "-Command", "Start-Sleep -Seconds 60"]
    )
    state_path = tmp_path / "coordinated-process.json"
    try:
        _write_process_state(state_path, "session-1", child)
        history = GatewayHistory()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(history.handle)
        ) as http:
            runs = repository(history, http)
            await runs.start(request(tmp_path))
            result = await CodexCancellationCoordinator(
                repository=runs,
                worker_id="worker-1",
                pwsh_path=Path(_pwsh()),
                script_path=Path("scripts/codex-session.ps1").resolve(),
            ).cancel(
                session_id="session-1",
                state_path=state_path,
                reason="operator",
            )

        child.wait(timeout=10)
        assert result == CodexCancellationResult(
            session_id="session-1",
            outcome="cancelled",
            cancellation_reason="operator",
        )
        assert history.events[-1]["event_type"] == "codex_session_finished"
        assert history.events[-1]["payload"]["cancellation_reason"] == "operator"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
async def test_session_bound_canceller_persists_a_captain_revocation(
    tmp_path: Path,
) -> None:
    child = subprocess.Popen(
        [_pwsh(), "-NoProfile", "-Command", "Start-Sleep -Seconds 60"]
    )
    state_path = tmp_path / "captain-revoked-process.json"
    try:
        _write_process_state(state_path, "session-1", child)
        history = GatewayHistory()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(history.handle)
        ) as http:
            runs = repository(history, http)
            await runs.start(request(tmp_path))
            canceller = SessionBoundCodexCanceller(
                coordinator=CodexCancellationCoordinator(
                    repository=runs,
                    worker_id="worker-1",
                    pwsh_path=Path(_pwsh()),
                    script_path=Path("scripts/codex-session.ps1").resolve(),
                ),
                session_id="session-1",
                state_path=state_path,
            )
            await canceller.cancel()

        child.wait(timeout=10)
        assert history.events[-1]["event_type"] == "codex_session_finished"
        assert history.events[-1]["payload"]["outcome"] == "cancelled"
        assert history.events[-1]["payload"]["cancellation_reason"] == "captain_revoked"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
async def test_cancellation_coordinator_persistence_failure_is_recoverable_not_complete(
    tmp_path: Path,
) -> None:
    child = subprocess.Popen(
        [_pwsh(), "-NoProfile", "-Command", "Start-Sleep -Seconds 60"]
    )
    state_path = tmp_path / "unresolved-process.json"
    history = GatewayHistory()

    def fail_terminal(request_: httpx.Request) -> httpx.Response:
        if request_.method == "POST":
            body = json.loads(request_.content)
            if body["event_type"] == "codex_session_finished":
                return httpx.Response(503, json={"detail": "down"}, request=request_)
        return history.handle(request_)

    try:
        _write_process_state(state_path, "session-1", child)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail_terminal)
        ) as http:
            runs = repository(history, http)
            await runs.start(request(tmp_path))
            coordinator = CodexCancellationCoordinator(
                repository=runs,
                worker_id="worker-1",
                pwsh_path=Path(_pwsh()),
                script_path=Path("scripts/codex-session.ps1").resolve(),
            )
            with pytest.raises(CancellationPersistenceRequired):
                await coordinator.cancel(
                    session_id="session-1",
                    state_path=state_path,
                    reason="shutdown",
                )
            assert len(await runs.active(worker_id="worker-1")) == 1

        child.wait(timeout=10)
        assert all(
            event["event_type"] != "codex_session_finished"
            for event in history.events
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
async def test_restart_replay_uses_original_start_time_and_rejects_terminal_conflict(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        first = repository(history, http)
        original = await first.start(request(tmp_path))

        later = GatewayCodexRunRepository(
            client=GatewayDeliveryClient("http://gateway", "worker-secret", http),
            project_id="project-1",
            run_id="run-1",
            actor="worker-1",
            now=lambda: NOW + timedelta(minutes=5),
        )
        with pytest.raises(ActiveCodexSessionRecoveryRequired, match="requires recovery"):
            await later.start(request(tmp_path))
        await later.finish(
            "session-1", CodexOutcome(classification="succeeded")
        )
        with pytest.raises(ValueError, match="terminal outcome conflicts"):
            await later.finish(
                "session-1",
                CodexOutcome(
                    classification="cancelled",
                    cancellation_reason="operator",
                ),
            )


@pytest.mark.asyncio
async def test_gateway_repository_implements_task3b_record_codex_event_port(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)
        await runs.start(request(tmp_path))
        await runs.record_codex_event(
            "batch-1",
            "claim-secret",
            iteration=1,
            session_id="session-1",
            event=CodexProcessEvent(
                lifecycle="turn_started", source_sequence=0
            ),
        )

    assert history.events[-1]["event_type"] == "codex_session_event"
    assert "claim-secret" not in json.dumps(history.events)


@pytest.mark.asyncio
async def test_gateway_repository_preserves_external_codex_thread_id(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)
        await runs.start(request(tmp_path))
        await runs.append(
            CodexProcessEvent(
                lifecycle="started",
                session_id="codex-thread-1",
                source_sequence=0,
            )
        )

    event = history.events[-1]
    assert event["payload"]["session_id"] == "session-1"
    assert event["payload"]["external_session_id"] == "codex-thread-1"


@pytest.mark.asyncio
async def test_identical_lifecycle_lines_use_sequence_for_distinct_idempotent_events(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)
        await runs.start(request(tmp_path))
        first = CodexProcessEvent(lifecycle="turn_started", source_sequence=0)
        second = CodexProcessEvent(lifecycle="turn_started", source_sequence=1)
        await runs.append(first)
        await runs.append(second)
        await runs.append(first)

    lifecycle = [
        event for event in history.events
        if event["event_type"] == "codex_session_event"
    ]
    assert len(lifecycle) == 2
    assert lifecycle[0]["event_id"] != lifecycle[1]["event_id"]
    assert [event["payload"]["source_sequence"] for event in lifecycle] == [0, 1]


@pytest.mark.asyncio
async def test_supervisor_terminalizes_infrastructure_when_event_append_fails(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()

    def failing_append(request_: httpx.Request) -> httpx.Response:
        if request_.method == "POST":
            body = json.loads(request_.content)
            if body["event_type"] == "codex_session_event":
                return httpx.Response(503, json={"detail": "down"}, request=request_)
        return history.handle(request_)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(failing_append)
    ) as http:
        runs = repository(history, http)

        class Runner:
            async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
                return _durable_codex_result(
                    journal_path=tmp_path / "runner.jsonl",
                    exit_code=0,
                    terminal_status="succeeded",
                    artifact_references=(),
                    jsonl_lines=('{"type":"turn.started"}',),
                )

        result = await CodexSupervisor(
            runner=Runner(),
            gateway=ForbiddenLegacyWriter(),
            policy=AllowingPolicy(),
            repository=runs,
        ).run(request(tmp_path))

    assert result.status is PackageExecutionStatus.FAILED
    assert history.events[-1]["event_type"] == "codex_session_finished"
    assert history.events[-1]["payload"]["outcome"] == "infrastructure_failure"
    assert history.events[-1]["payload"]["behavioral_repair_increment"] == 0



@pytest.mark.asyncio
async def test_supervisor_reports_unresolved_evidence_when_lifecycle_and_terminal_fail(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()

    def fail_lifecycle_and_terminal(request_: httpx.Request) -> httpx.Response:
        if request_.method == "POST":
            body = json.loads(request_.content)
            if body["event_type"] in {
                "codex_session_event",
                "codex_session_finished",
            }:
                return httpx.Response(503, json={"detail": "down"}, request=request_)
        return history.handle(request_)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fail_lifecycle_and_terminal)
    ) as http:
        runs = repository(history, http)

        class Runner:
            async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
                return _durable_codex_result(
                    journal_path=tmp_path / "runner.jsonl",
                    exit_code=0,
                    terminal_status="succeeded",
                    artifact_references=(),
                    jsonl_lines=('{"type":"turn.started"}',),
                )

        result = await CodexSupervisor(
            runner=Runner(),
            gateway=ForbiddenLegacyWriter(),
            policy=AllowingPolicy(),
            repository=runs,
        ).run(request(tmp_path))

        assert result.status is PackageExecutionStatus.EVIDENCE_UNRESOLVED
        assert result.error == "codex terminal evidence requires recovery"
        assert len(await runs.active(worker_id="worker-1")) == 1


@pytest.mark.asyncio
async def test_supervisor_assigns_stable_source_sequence_to_each_jsonl_line(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)

        class Runner:
            async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
                return _durable_codex_result(
                    journal_path=tmp_path / "runner.jsonl",
                    exit_code=0,
                    terminal_status="succeeded",
                    artifact_references=("artifact://sealed/sequence",),
                    jsonl_lines=(
                        '{"type":"turn.started"}',
                        '{"type":"turn.started"}',
                    ),
                )

        await CodexSupervisor(
            runner=Runner(),
            gateway=ForbiddenLegacyWriter(),
            policy=AllowingPolicy(),
            repository=runs,
        ).run(request(tmp_path))

    lifecycle = [
        event["payload"] for event in history.events
        if event["event_type"] == "codex_session_event"
    ]
    assert [payload["source_sequence"] for payload in lifecycle] == [0, 1]


@pytest.mark.asyncio
async def test_active_and_reconcile_reject_conflicting_terminal_history(
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)
        await runs.start(request(tmp_path))
        await runs.finish("session-1", CodexOutcome(classification="succeeded"))
        conflicting = json.loads(json.dumps(history.events[-1]))
        conflicting["event_id"] = str(uuid4())
        conflicting["payload"]["outcome"] = "cancelled"
        conflicting["payload"]["cancellation_reason"] = "operator"
        history.events.append(conflicting)

        restarted = repository(history, http)
        with pytest.raises(ValueError, match="conflicting terminal"):
            await restarted.active(worker_id="worker-1")
        with pytest.raises(ValueError, match="conflicting terminal"):
            await restarted.reconcile(
                worker_id="worker-1", live_process_ids=frozenset()
            )

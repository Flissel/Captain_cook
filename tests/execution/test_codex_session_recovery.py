from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest

from agenten.delivery.codex_runs import (
    CodexOutcome,
    GatewayCodexRunRepository,
)
from agenten.delivery.gateway_client import GatewayDeliveryClient
from agenten.execution.codex_events import CodexParseWarning, CodexProcessEvent
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment
from agenten.execution.codex_supervisor import (
    CodexRunRequest,
    CodexRunResult,
    CodexSupervisor,
)
from agenten.execution.process import PackageExecutionStatus


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


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
                return CodexRunResult(
                    exit_code=0,
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
async def test_cancellation_and_infrastructure_outcomes_never_consume_repair(
    tmp_path: Path,
) -> None:
    history = GatewayHistory()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(history.handle)
    ) as http:
        runs = repository(history, http)
        await runs.start(request(tmp_path))
        await runs.finish(
            "session-1",
            CodexOutcome(
                classification="cancelled",
                cancellation_reason="operator",
            ),
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
        assert await runs.start(request(tmp_path, prompt=raw_prompt)) == first
        event = CodexProcessEvent(lifecycle="failed")
        warning = CodexParseWarning(
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
                return CodexRunResult(
                    exit_code=23,
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


def test_powershell_launcher_uses_argument_array_and_exact_tree_cancellation() -> None:
    script = Path("scripts/codex-session.ps1").read_text(encoding="utf-8")

    assert "Get-Command -Name codex" in script
    assert '.ArgumentList.Add("exec")' in script
    assert '.ArgumentList.Add("--json")' in script
    assert '.ArgumentList.Add($Prompt)' in script
    assert "taskkill.exe" in script
    assert '"/PID", "$CancelProcessId", "/T", "/F"' in script
    assert "exit $process.ExitCode" in script
    assert "Get-ChildItem Env:" not in script


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
        assert await later.start(request(tmp_path)) == original
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
            event=CodexProcessEvent(lifecycle="turn_started"),
        )

    assert history.events[-1]["event_type"] == "codex_session_event"
    assert "claim-secret" not in json.dumps(history.events)

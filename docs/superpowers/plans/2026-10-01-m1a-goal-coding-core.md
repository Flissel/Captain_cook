# M1a: Goal-gesteuerter Coding-Kern (offline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Den spike-unabhängigen Kern bauen, mit dem AutoGen-Agenten Claude Code CLI als Goal-gesteuertes Produktionswerkzeug in einer Sandbox nutzen — Goal-Verträge, Claude-Stream-Parser, Goal-Schleife gegen einen Sandbox-Port, die Tools `claude_code_build`/`sandbox_run_tests`, die reinen Relay-Übersetzungen Anthropic↔Chat und das Team-Schema `graph_flow` — vollständig offline getestet.

**Architecture:** Neues Paket `agenten/coding_agent/` mit klaren Ports: `SandboxPort` (später ConTree, hier Fake), `BudgetGuardPort`, `CodingToolAuthorityPort`, `RunTokenIssuerPort`, `GoalEvidenceSinkPort`. Die Goal-Schleife rendert aus `AgentGoalV1` einen Prompt, lässt Claude Code in der Sandbox laufen, prüft geänderte Pfade und lässt die Abnahmebefehle unabhängig laufen; bei Rot folgt `--resume`. Das Relay bekommt reine Übersetzungsfunktionen in `agenten/provider_proxy/anthropic_bridge.py`; die HTTP-Verdrahtung folgt in M1b. `graph_flow` erweitert das bestehende Team-Manifest und `team_execution.py` über ein kleines Builder-Modul.

**Tech Stack:** Python 3.11, pydantic v2, autogen-agentchat/autogen-core 0.7.5 (`GraphFlow`, `DiGraphBuilder`, `BaseTool`), pytest (+ pytest-asyncio wie im Repo).

**Spec:** `docs/superpowers/specs/2026-09-30-goal-driven-agent-builder-design.md` (Abschnitte 4, 5, 7, 8) und `docs/superpowers/specs/2026-09-29-nebius-codex-replacement-design.md` (Abschnitte 4, 5).

**Nicht in diesem Plan (M1b, nach dem Spike):** echter ConTree-Adapter, Workspace-PREPARE, Nebius-Chat-Transport, `/v1/messages`-Route und Run-Token-Ausgabe im Provider-Proxy, Ledger-Verdrahtung der Goal-Evidenz, Live-Tests.

## Global Constraints

- Python 3.11; Tests mit `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider` aus dem Worktree-Root `C:\Users\User\Desktop\Captain_cook\.worktrees\nebius-claude-sandbox` (der Worktree hat kein eigenes `.venv`; nacktes `python` ist hier ein No-op-Shim).
- autogen-agentchat 0.7.5 (`from autogen_agentchat.teams import GraphFlow, DiGraphBuilder` ist verfügbar).
- Offline und deterministisch: keine Netzwerkaufrufe, keine echten Schlüssel, keine Live-Tests in diesem Plan.
- Geheimnisse: Das Run-Token darf in keinem `repr`, keinem Ergebnisobjekt, keinem Tool-Rückgabewert und keinem Log auftauchen; `NEBIUS_API_KEY`/`ANTHROPIC_API_KEY` werden nie an die Sandbox gegeben.
- Environment für Claude in der Sandbox ist genau: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`.
- Claude-Aufruf: `claude -p --output-format stream-json --verbose --model <m> --max-turns <n> --dangerously-skip-permissions --allowedTools Bash,Read,Edit,Write,Glob,Grep --setting-sources '' --strict-mcp-config [--resume <session>]`, Prompt über stdin.
- Reviewer-Verdikte in GraphFlow sind Text-Token `APPROVE`/`ITERATE` in der **ersten Zeile**; Manifest-Kantenbedingungen sind deklarativ (`always`/`approve`/`iterate`), nie Code.
- `artifacts/demo-run.json` nicht anfassen. Conventional Commits, Trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, kein Push, kein `--no-verify`. `.coverage.*`-Dateien vor dem Commit löschen, explizite Pfade stagen.
- Bekannte, schon vor diesem Plan rote Tests (nicht reparieren): `tests/agent_factory/test_hermes_skill_evaluation.py::test_absolute_deadline_cancels_final_store_operations_before_success[*]`, `tests/demo_orchestrator/test_configuration.py::test_real_d11_manifest_builder_is_compatible_with_d12_loader`, `tests/agent_runtime/test_generate_runtime_adapter_manifest.py` (3, brauchen repo-lokales `.venv`), flaky `tests/execution/test_codex_session_recovery.py::test_powershell_runner_timeout_retains_durable_journal` und `test_dispatch_timeout_terminates_hermes_process_tree`.

## Review Focus

- Claude ändert Dateien außerhalb von `allowed_paths` → Runde endet als `policy_violation`, Abnahme läuft nicht, der Workspace-Cursor rückt nicht vor (Tests in Task 5 und Task 6).
- Claude-Ausgabe ohne `session_id`/`result` (Absturz, Relay-Fehler, Auth-Fehler) → Status `failed` mit Grund, keine weitere Runde (Test in Task 5).
- Reviewer schreibt „ITERATE … statt APPROVE“ → nur die erste Zeile entscheidet; kein Doppel-Feuern beider Kanten (Test in Task 9).
- Tool-Namen über 64 Zeichen → kommen im Stream als Original-Name bei Claude an (Tests in Task 7 und Task 8).
- Run-Token taucht weder in `repr(SandboxCommand)` noch in `AgentGoalResultV1` noch im Tool-Ergebnis auf (Tests in Task 3, Task 5, Task 6).

---

### Task 1: Goal-Verträge

**Files:**
- Create: `agenten/coding_agent/__init__.py`, `agenten/coding_agent/contracts.py`
- Test: `tests/coding_agent/__init__.py`, `tests/coding_agent/test_contracts.py`

**Interfaces:**
- Produces: `AgentGoalV1`, `AcceptanceCheckV1`, `AcceptanceReportV1` (Property `passed: bool`, Methode `failing() -> tuple[AcceptanceCheckV1, ...]`), `AgentGoalRoundV1`, `AgentGoalResultV1`, `GoalStatus` (Literal), `path_allowed(path: str, allowed: Sequence[str]) -> bool`, Konstante `ID_PATTERN`.

- [ ] **Step 1: Failing tests** — `tests/coding_agent/test_contracts.py` (dazu leere `tests/coding_agent/__init__.py`):

```python
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agenten.coding_agent.contracts import (
    AcceptanceCheckV1,
    AcceptanceReportV1,
    AgentGoalResultV1,
    AgentGoalV1,
    path_allowed,
)


def _goal(**overrides):
    values = {
        "goal_id": "team-1-build-1",
        "objective": "Add a hello() function returning 'hi'.",
        "acceptance_commands": ("pytest -q tests/test_hello.py",),
        "allowed_paths": ("src/", "tests/test_hello.py"),
        "max_rounds": 3,
        "max_turns_per_round": 30,
        "budget_usd": Decimal("1.50"),
        "timeout_seconds": 900,
    }
    values.update(overrides)
    return AgentGoalV1(**values)


def test_goal_round_trips_with_schema_alias():
    goal = _goal()
    dumped = goal.model_dump(mode="json", by_alias=True)
    assert dumped["schema"] == "captain.agent-goal.v1"
    assert AgentGoalV1.model_validate(dumped) == goal


@pytest.mark.parametrize("command", ["", "  ", "pytest\nrm -rf /", "a\x00b"])
def test_acceptance_commands_must_be_single_non_empty_lines(command):
    with pytest.raises(ValidationError):
        _goal(acceptance_commands=(command,))


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "src/../../x", "C:\\x"])
def test_allowed_paths_must_be_safe_relative(path):
    with pytest.raises(ValidationError):
        _goal(allowed_paths=(path,))


@pytest.mark.parametrize(
    "field,value",
    [("max_rounds", 0), ("max_rounds", 11), ("max_turns_per_round", 0), ("timeout_seconds", 59), ("budget_usd", Decimal("0"))],
)
def test_limits_are_bounded(field, value):
    with pytest.raises(ValidationError):
        _goal(**{field: value})


def test_goal_is_frozen():
    with pytest.raises(ValidationError):
        _goal().objective = "other"  # type: ignore[misc]


def test_path_allowed_matches_files_directories_and_dot():
    assert path_allowed("src/app.py", ("src/",))
    assert path_allowed("src/app.py", ("src",))
    assert path_allowed("tests/test_hello.py", ("tests/test_hello.py",))
    assert not path_allowed("srcx/app.py", ("src",))
    assert not path_allowed("tests/test_other.py", ("tests/test_hello.py",))
    assert path_allowed("anything/at/all.txt", (".",))


def test_report_passed_and_failing():
    ok = AcceptanceCheckV1(command="a", exit_code=0, timed_out=False, passed=True, output_tail="")
    bad = AcceptanceCheckV1(command="b", exit_code=1, timed_out=False, passed=False, output_tail="E")
    report = AcceptanceReportV1(checks=(ok, bad))
    assert report.passed is False
    assert report.failing() == (bad,)
    assert AcceptanceReportV1(checks=(ok,)).passed is True


def test_check_passed_must_match_exit_code():
    with pytest.raises(ValidationError):
        AcceptanceCheckV1(command="a", exit_code=1, timed_out=False, passed=True, output_tail="")


def test_result_requires_known_status():
    with pytest.raises(ValidationError):
        AgentGoalResultV1(goal_id="g-1", status="done", rounds=(), final_image_id="img", failure_reason=None)
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_contracts.py`
Expected: FAIL mit `ModuleNotFoundError: No module named 'agenten.coding_agent'`

- [ ] **Step 3: Implementieren** — `agenten/coding_agent/__init__.py`:

```python
"""Goal-driven coding agents (Claude Code CLI) that run inside sandboxes."""
```

`agenten/coding_agent/contracts.py`:

```python
"""Goal contracts for coding agents that run in sandboxes.

An `AgentGoalV1` is what a team agent hands to the coding tool: an objective,
the commands that judge it, and hard limits. The runner never trusts the
coding agent's own claim of success; the acceptance commands decide.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SHA256 = r"^[0-9a-f]{64}$"

GoalStatus = Literal[
    "achieved",
    "exhausted",
    "timeout",
    "budget_exhausted",
    "policy_violation",
    "failed",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _single_line(value: str, *, limit: int) -> str:
    if not value.strip() or len(value) > limit or any(ch in value for ch in "\x00\r\n"):
        raise ValueError("must be a non-empty single line")
    return value


def _safe_relative(value: str) -> str:
    if "\\" in value or ":" in value:
        raise ValueError("path must use forward slashes and no drive")
    path = PurePosixPath(value)
    if value != "." and (path.is_absolute() or ".." in path.parts or not value.strip()):
        raise ValueError("path must be a safe relative path")
    return value


def path_allowed(path: str, allowed: Sequence[str]) -> bool:
    """True when `path` equals an allowed file or lies under an allowed directory."""
    for entry in allowed:
        if entry == ".":
            return True
        base = entry.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


class AgentGoalV1(_Frozen):
    schema_name: Literal["captain.agent-goal.v1"] = Field(
        default="captain.agent-goal.v1", alias="schema"
    )
    goal_id: str = Field(pattern=ID_PATTERN)
    parent_goal_ref: str | None = Field(default=None, pattern=ID_PATTERN)
    objective: str = Field(min_length=1, max_length=8000)
    acceptance_commands: tuple[str, ...] = Field(min_length=1, max_length=20)
    constraints: tuple[str, ...] = Field(default=(), max_length=30)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=50)
    max_rounds: int = Field(ge=1, le=10, strict=True)
    max_turns_per_round: int = Field(ge=1, le=200, strict=True)
    budget_usd: Decimal = Field(gt=Decimal("0"), le=Decimal("50"))
    timeout_seconds: int = Field(ge=60, le=7200, strict=True)

    @field_validator("acceptance_commands")
    @classmethod
    def _commands(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_single_line(item, limit=2000) for item in value)

    @field_validator("constraints")
    @classmethod
    def _constraints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_single_line(item, limit=500) for item in value)

    @field_validator("allowed_paths")
    @classmethod
    def _paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_safe_relative(item) for item in value)


class AcceptanceCheckV1(_Frozen):
    command: str = Field(min_length=1, max_length=2000)
    exit_code: int = Field(strict=True)
    timed_out: bool
    passed: bool
    output_tail: str = Field(max_length=4000)

    @model_validator(mode="after")
    def _consistent(self) -> "AcceptanceCheckV1":
        if self.passed != (self.exit_code == 0 and not self.timed_out):
            raise ValueError("passed must equal exit_code == 0 and not timed_out")
        return self


class AcceptanceReportV1(_Frozen):
    checks: tuple[AcceptanceCheckV1, ...] = Field(min_length=1)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failing(self) -> tuple[AcceptanceCheckV1, ...]:
        return tuple(check for check in self.checks if not check.passed)


class AgentGoalRoundV1(_Frozen):
    round: int = Field(ge=1, strict=True)
    input_image_id: str = Field(min_length=1)
    output_image_id: str = Field(min_length=1)
    session_id: str | None = None
    result_subtype: str | None = None
    num_turns: int = Field(ge=0, strict=True)
    tool_call_count: int = Field(ge=0, strict=True)
    exit_code: int = Field(strict=True)
    timed_out: bool
    journal_sha256: str = Field(pattern=_SHA256)
    changed_paths: tuple[str, ...] = ()
    policy_violations: tuple[str, ...] = ()
    acceptance: AcceptanceReportV1 | None = None


class AgentGoalResultV1(_Frozen):
    schema_name: Literal["captain.agent-goal-result.v1"] = Field(
        default="captain.agent-goal-result.v1", alias="schema"
    )
    goal_id: str = Field(pattern=ID_PATTERN)
    status: GoalStatus
    rounds: tuple[AgentGoalRoundV1, ...]
    final_image_id: str = Field(min_length=1)
    failure_reason: str | None = Field(default=None, max_length=300)
```

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_contracts.py`
Expected: alle grün

- [ ] **Step 5: Commit**

```bash
git add agenten/coding_agent/__init__.py agenten/coding_agent/contracts.py tests/coding_agent/__init__.py tests/coding_agent/test_contracts.py
git commit -m "feat(coding-agent): add goal, acceptance and round contracts"
```

### Task 2: Claude-`stream-json`-Parser

**Files:**
- Create: `agenten/coding_agent/claude_stream.py`
- Test: `tests/coding_agent/test_claude_stream.py`

**Interfaces:**
- Produces: `AgentEventV2`, `ClaudeStreamWarning`, `ClaudeRunSummary`, `ClaudeStreamTooLargeError(ValueError)`, `parse_claude_stream_line(line: str) -> tuple[AgentEventV2 | ClaudeStreamWarning, ...]`, `summarize_claude_stream(stdout: str, *, max_bytes: int = 10 * 1024 * 1024) -> ClaudeRunSummary`.

- [ ] **Step 1: Failing tests**

```python
import json

import pytest

from agenten.coding_agent.claude_stream import (
    AgentEventV2,
    ClaudeStreamTooLargeError,
    ClaudeStreamWarning,
    parse_claude_stream_line,
    summarize_claude_stream,
)

INIT = {"type": "system", "subtype": "init", "session_id": "s-1", "model": "nvidia/x", "tools": ["Bash"]}
ASSIST_TOOL = {
    "type": "assistant",
    "session_id": "s-1",
    "message": {"content": [
        {"type": "text", "text": "I will write the file"},
        {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {"file_path": "a.py", "content": "SECRET-CONTENT"}},
    ]},
}
TOOL_RESULT = {
    "type": "user",
    "session_id": "s-1",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok", "is_error": True}]},
}
ASSIST_TEXT = {"type": "assistant", "session_id": "s-1", "message": {"content": [{"type": "text", "text": "done"}]}}
RESULT = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "session_id": "s-1",
    "total_cost_usd": 9.99, "usage": {"input_tokens": 120, "output_tokens": 45}, "result": "done",
}


def _lines(*records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def test_init_becomes_started():
    (event,) = parse_claude_stream_line(json.dumps(INIT))
    assert event == AgentEventV2(lifecycle="started", session_id="s-1")


def test_assistant_tool_use_becomes_tool_call_without_content():
    (event,) = parse_claude_stream_line(json.dumps(ASSIST_TOOL))
    assert event.lifecycle == "tool_call"
    assert event.item_id == "toolu_1"
    assert event.item_type == "Write"
    assert "SECRET-CONTENT" not in event.model_dump_json()


def test_assistant_text_only_becomes_turn_completed():
    (event,) = parse_claude_stream_line(json.dumps(ASSIST_TEXT))
    assert event.lifecycle == "turn_completed"


def test_tool_result_carries_error_flag():
    (event,) = parse_claude_stream_line(json.dumps(TOOL_RESULT))
    assert event.lifecycle == "tool_result"
    assert event.is_error is True


def test_result_success_and_error_subtypes():
    (ok,) = parse_claude_stream_line(json.dumps(RESULT))
    assert ok.lifecycle == "completed"
    assert (ok.num_turns, ok.input_tokens, ok.output_tokens) == (3, 120, 45)
    failed = dict(RESULT, subtype="error_max_turns", is_error=True)
    (bad,) = parse_claude_stream_line(json.dumps(failed))
    assert bad.lifecycle == "failed"
    assert bad.result_subtype == "error_max_turns"


@pytest.mark.parametrize(
    "line,kind",
    [("not json", "malformed_json"), ("[1,2]", "invalid_event"), (json.dumps({"type": "mystery"}), "unknown_event")],
)
def test_bad_lines_become_warnings(line, kind):
    (warning,) = parse_claude_stream_line(line)
    assert isinstance(warning, ClaudeStreamWarning)
    assert warning.warning_type == kind
    assert len(warning.line_sha256) == 64


def test_summary_counts_and_hashes():
    stdout = _lines(INIT, ASSIST_TOOL, TOOL_RESULT, ASSIST_TEXT, RESULT) + "garbage\n"
    summary = summarize_claude_stream(stdout)
    assert summary.session_id == "s-1"
    assert summary.result_subtype == "success"
    assert summary.num_turns == 3
    assert summary.tool_call_count == 1
    assert summary.tool_error_count == 1
    assert (summary.input_tokens, summary.output_tokens) == (120, 45)
    assert summary.warning_count == 1
    assert len(summary.journal_sha256) == 64


def test_summary_without_result_has_no_subtype():
    summary = summarize_claude_stream(_lines(INIT, ASSIST_TEXT))
    assert summary.session_id == "s-1"
    assert summary.result_subtype is None


def test_empty_stream_has_no_session():
    assert summarize_claude_stream("").session_id is None


def test_oversized_stream_is_rejected():
    with pytest.raises(ClaudeStreamTooLargeError):
        summarize_claude_stream("x" * 101, max_bytes=100)
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_claude_stream.py`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implementieren** — `agenten/coding_agent/claude_stream.py`:

```python
"""Secret-safe parsing of Claude Code `--output-format stream-json` output.

Only types, ids, counters and hashes survive; message text, tool inputs and
tool outputs are never retained. Claude's own cost figure is ignored because
the provider proxy settlement is authoritative.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Lifecycle = Literal["started", "tool_call", "tool_result", "turn_completed", "completed", "failed"]


class ClaudeStreamTooLargeError(ValueError):
    """The stream exceeds the journal size ceiling."""


class AgentEventV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal["agent_session"] = "agent_session"
    lifecycle: Lifecycle
    session_id: str | None = None
    item_id: str | None = None
    item_type: str | None = None
    is_error: bool | None = None
    result_subtype: str | None = None
    num_turns: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class ClaudeStreamWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warning_type: Literal["malformed_json", "unknown_event", "invalid_event"]
    line_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClaudeRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str | None
    result_subtype: str | None
    num_turns: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    tool_error_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    journal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _content(record: dict[str, object]) -> list[object] | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return []
    return content if isinstance(content, list) else None


def parse_claude_stream_line(line: str) -> tuple[AgentEventV2 | ClaudeStreamWarning, ...]:
    digest = hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()

    def warn(kind: Literal["malformed_json", "unknown_event", "invalid_event"]) -> tuple[ClaudeStreamWarning]:
        return (ClaudeStreamWarning(warning_type=kind, line_sha256=digest),)

    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return warn("malformed_json")
    if not isinstance(record, dict):
        return warn("invalid_event")
    kind = record.get("type")
    session = _text(record.get("session_id"))

    if kind == "system":
        if record.get("subtype") == "init":
            return (AgentEventV2(lifecycle="started", session_id=session),)
        return ()
    if kind == "assistant":
        content = _content(record)
        if content is None:
            return warn("invalid_event")
        calls = tuple(
            AgentEventV2(
                lifecycle="tool_call",
                session_id=session,
                item_id=_text(block.get("id")),
                item_type=_text(block.get("name")),
            )
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        )
        return calls or (AgentEventV2(lifecycle="turn_completed", session_id=session),)
    if kind == "user":
        content = _content(record)
        if content is None:
            return warn("invalid_event")
        return tuple(
            AgentEventV2(
                lifecycle="tool_result",
                session_id=session,
                item_id=_text(block.get("tool_use_id")),
                is_error=bool(block.get("is_error", False)),
            )
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
    if kind == "result":
        subtype = _text(record.get("subtype"))
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        assert isinstance(usage, dict)
        failed = bool(record.get("is_error")) or subtype != "success"
        return (
            AgentEventV2(
                lifecycle="failed" if failed else "completed",
                session_id=session,
                result_subtype=subtype,
                num_turns=_count(record.get("num_turns")),
                input_tokens=_count(usage.get("input_tokens")),
                output_tokens=_count(usage.get("output_tokens")),
            ),
        )
    return warn("unknown_event")


def summarize_claude_stream(stdout: str, *, max_bytes: int = 10 * 1024 * 1024) -> ClaudeRunSummary:
    data = stdout.encode("utf-8", "replace")
    if len(data) > max_bytes:
        raise ClaudeStreamTooLargeError(f"claude stream exceeds {max_bytes} bytes")
    session: str | None = None
    subtype: str | None = None
    num_turns = tool_calls = tool_errors = input_tokens = output_tokens = warnings = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        for item in parse_claude_stream_line(line):
            if isinstance(item, ClaudeStreamWarning):
                warnings += 1
                continue
            session = item.session_id or session
            if item.lifecycle == "tool_call":
                tool_calls += 1
            elif item.lifecycle == "tool_result" and item.is_error:
                tool_errors += 1
            elif item.lifecycle in ("completed", "failed"):
                subtype = item.result_subtype
                num_turns = item.num_turns or 0
                input_tokens = item.input_tokens or 0
                output_tokens = item.output_tokens or 0
    return ClaudeRunSummary(
        session_id=session,
        result_subtype=subtype,
        num_turns=num_turns,
        tool_call_count=tool_calls,
        tool_error_count=tool_errors,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        warning_count=warnings,
        journal_sha256=hashlib.sha256(data).hexdigest(),
    )
```

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_claude_stream.py`
Expected: alle grün

- [ ] **Step 5: Commit**

```bash
git add agenten/coding_agent/claude_stream.py tests/coding_agent/test_claude_stream.py
git commit -m "feat(coding-agent): parse Claude Code stream-json into secret-safe events"
```

### Task 3: Sandbox-Port, Abnahme und geänderte Pfade

**Files:**
- Create: `agenten/coding_agent/sandbox.py`, `agenten/coding_agent/acceptance.py`
- Create (Test-Fake): `tests/coding_agent/fakes.py`
- Test: `tests/coding_agent/test_acceptance.py`

**Interfaces:**
- Consumes: `AcceptanceCheckV1`, `AcceptanceReportV1` (Task 1)
- Produces:
  - `@dataclass(frozen=True) SandboxCommand(shell: str, timeout_seconds: int, env: Mapping[str, str] = {} (repr=False), stdin: str | None = None (repr=False))`
  - `@dataclass(frozen=True) SandboxRunResult(exit_code: int, stdout: str, stderr: str, timed_out: bool, output_image_id: str)`
  - `class SandboxPort(Protocol): async def run(self, image_id: str, command: SandboxCommand) -> SandboxRunResult`
  - `class SandboxProtocolError(RuntimeError)`
  - `async def run_acceptance(sandbox: SandboxPort, *, image_id: str, commands: Sequence[str], workdir: str, timeout_seconds: int) -> AcceptanceReportV1`
  - `async def changed_paths(sandbox: SandboxPort, *, image_id: str, workdir: str) -> tuple[str, ...]`
  - Test-Fake `FakeSandbox(handler: Callable[[str, SandboxCommand], SandboxRunResult])` mit `calls: list[tuple[str, SandboxCommand]]`

- [ ] **Step 1: Test-Fake** — `tests/coding_agent/fakes.py`:

```python
"""Deterministic sandbox fake for coding-agent tests."""
from __future__ import annotations

from collections.abc import Callable

from agenten.coding_agent.sandbox import SandboxCommand, SandboxRunResult


class FakeSandbox:
    def __init__(self, handler: Callable[[str, SandboxCommand], SandboxRunResult]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, SandboxCommand]] = []

    async def run(self, image_id: str, command: SandboxCommand) -> SandboxRunResult:
        self.calls.append((image_id, command))
        return self._handler(image_id, command)


def ok(stdout: str = "", *, image: str = "img-out", exit_code: int = 0, timed_out: bool = False) -> SandboxRunResult:
    return SandboxRunResult(exit_code=exit_code, stdout=stdout, stderr="", timed_out=timed_out, output_image_id=image)
```

- [ ] **Step 2: Failing tests** — `tests/coding_agent/test_acceptance.py`:

```python
import pytest

from agenten.coding_agent.acceptance import changed_paths, run_acceptance
from agenten.coding_agent.sandbox import SandboxCommand, SandboxProtocolError
from tests.coding_agent.fakes import FakeSandbox, ok


@pytest.mark.asyncio
async def test_each_command_runs_in_workdir_and_is_judged_by_exit_code():
    def handler(image, command):
        if "fails" in command.shell:
            return ok("E" * 5000, exit_code=1)
        return ok("passed")

    sandbox = FakeSandbox(handler)
    report = await run_acceptance(
        sandbox, image_id="img-1", commands=("pytest -q a", "pytest -q fails"), workdir="/work", timeout_seconds=60
    )

    assert [c.passed for c in report.checks] == [True, False]
    assert report.passed is False
    assert len(report.checks[1].output_tail) == 2000
    assert all(img == "img-1" for img, _ in sandbox.calls)
    assert sandbox.calls[0][1].shell == "cd /work && pytest -q a"
    assert sandbox.calls[0][1].env == {}


@pytest.mark.asyncio
async def test_timeout_fails_the_check():
    sandbox = FakeSandbox(lambda image, command: ok(exit_code=0, timed_out=True))
    report = await run_acceptance(sandbox, image_id="i", commands=("x",), workdir="/work", timeout_seconds=5)
    assert report.checks[0].passed is False


@pytest.mark.asyncio
async def test_changed_paths_parses_porcelain_including_renames_and_quotes():
    porcelain = ' M src/app.py\n?? tests/test_new.py\nR  old.py -> src/new.py\n?? "weird name.txt"\n'
    sandbox = FakeSandbox(lambda image, command: ok(porcelain))
    paths = await changed_paths(sandbox, image_id="i", workdir="/work")
    assert paths == ("src/app.py", "src/new.py", "tests/test_new.py", "weird name.txt")
    assert sandbox.calls[0][1].shell == "git -C /work status --porcelain=v1 -uall"


@pytest.mark.asyncio
async def test_changed_paths_requires_a_git_workspace():
    sandbox = FakeSandbox(lambda image, command: ok("fatal: not a git repository", exit_code=128))
    with pytest.raises(SandboxProtocolError):
        await changed_paths(sandbox, image_id="i", workdir="/work")


def test_sandbox_command_repr_hides_env_and_stdin():
    command = SandboxCommand(shell="claude -p", timeout_seconds=60, env={"ANTHROPIC_AUTH_TOKEN": "tok-secret"}, stdin="prompt")
    assert "tok-secret" not in repr(command)
    assert "prompt" not in repr(command)
```

Vorher prüfen, wie async-Tests im Repo markiert werden (`git grep -n "asyncio_mode\|pytest.mark.asyncio" pytest.ini tests/test_demo.py`); falls `asyncio_mode = auto` gilt, die Marker trotzdem stehen lassen (harmlos).

- [ ] **Step 3: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_acceptance.py`
Expected: FAIL (`ModuleNotFoundError: agenten.coding_agent.sandbox`)

- [ ] **Step 4: Implementieren** — `agenten/coding_agent/sandbox.py`:

```python
"""Port for sandboxes that run one shell command on an image and return a new image."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


class SandboxProtocolError(RuntimeError):
    """The sandbox or workspace violated an expectation of the coding runner."""


@dataclass(frozen=True)
class SandboxCommand:
    shell: str
    timeout_seconds: int
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    stdin: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class SandboxRunResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    output_image_id: str


class SandboxPort(Protocol):
    async def run(self, image_id: str, command: SandboxCommand) -> SandboxRunResult: ...
```

`agenten/coding_agent/acceptance.py`:

```python
"""Independent acceptance checks and change detection inside the sandbox."""
from __future__ import annotations

import shlex
from collections.abc import Sequence

from agenten.coding_agent.contracts import AcceptanceCheckV1, AcceptanceReportV1
from agenten.coding_agent.sandbox import SandboxCommand, SandboxPort, SandboxProtocolError

OUTPUT_TAIL_CHARS = 2000


async def run_acceptance(
    sandbox: SandboxPort,
    *,
    image_id: str,
    commands: Sequence[str],
    workdir: str,
    timeout_seconds: int,
) -> AcceptanceReportV1:
    checks: list[AcceptanceCheckV1] = []
    for command in commands:
        result = await sandbox.run(
            image_id,
            SandboxCommand(shell=f"cd {shlex.quote(workdir)} && {command}", timeout_seconds=timeout_seconds),
        )
        checks.append(
            AcceptanceCheckV1(
                command=command,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                passed=result.exit_code == 0 and not result.timed_out,
                output_tail=(result.stdout + result.stderr)[-OUTPUT_TAIL_CHARS:],
            )
        )
    return AcceptanceReportV1(checks=tuple(checks))


async def changed_paths(sandbox: SandboxPort, *, image_id: str, workdir: str) -> tuple[str, ...]:
    result = await sandbox.run(
        image_id,
        SandboxCommand(shell=f"git -C {shlex.quote(workdir)} status --porcelain=v1 -uall", timeout_seconds=60),
    )
    if result.exit_code != 0 or result.timed_out:
        raise SandboxProtocolError("workspace is not a readable git repository")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path.strip('"'))
    return tuple(sorted(paths))
```

- [ ] **Step 5: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_acceptance.py`
Expected: alle grün

- [ ] **Step 6: Commit**

```bash
git add agenten/coding_agent/sandbox.py agenten/coding_agent/acceptance.py tests/coding_agent/fakes.py tests/coding_agent/test_acceptance.py
git commit -m "feat(coding-agent): add sandbox port, acceptance checks and change detection"
```

### Task 4: Goal-Prompt-Vorlagen

**Files:**
- Create: `agenten/coding_agent/prompts/claude_goal_v1.md`, `agenten/coding_agent/prompts/claude_resume_v1.md`, `agenten/coding_agent/goal_prompt.py`
- Test: `tests/coding_agent/test_goal_prompt.py`

**Interfaces:**
- Consumes: `AgentGoalV1`, `AcceptanceReportV1` (Task 1)
- Produces: `render_goal_prompt(goal: AgentGoalV1, *, workdir: str) -> str`, `render_resume_prompt(report: AcceptanceReportV1) -> str`

- [ ] **Step 1: Failing tests**

```python
from decimal import Decimal

from agenten.coding_agent.contracts import AcceptanceCheckV1, AcceptanceReportV1, AgentGoalV1
from agenten.coding_agent.goal_prompt import render_goal_prompt, render_resume_prompt


def _goal():
    return AgentGoalV1(
        goal_id="g-1",
        objective="Add hello() returning 'hi' and cost $5 docs",
        acceptance_commands=("pytest -q tests/test_hello.py", "ruff check src"),
        constraints=("Keep public API stable",),
        allowed_paths=("src/", "tests/test_hello.py"),
        max_rounds=2,
        max_turns_per_round=20,
        budget_usd=Decimal("1"),
        timeout_seconds=600,
    )


def test_goal_prompt_lists_objective_commands_constraints_paths():
    text = render_goal_prompt(_goal(), workdir="/work")
    assert "Add hello() returning 'hi' and cost $5 docs" in text
    assert "- `pytest -q tests/test_hello.py`" in text
    assert "- `ruff check src`" in text
    assert "- Keep public API stable" in text
    assert "- `src/`" in text
    assert "/work" in text
    assert "until every acceptance command passes" in text


def test_goal_prompt_without_constraints_says_none():
    goal = _goal().model_copy(update={"constraints": ()})
    assert "- (none)" in render_goal_prompt(goal, workdir="/work")


def test_resume_prompt_lists_only_failing_checks_with_tails():
    report = AcceptanceReportV1(checks=(
        AcceptanceCheckV1(command="pytest -q a", exit_code=0, timed_out=False, passed=True, output_tail="fine"),
        AcceptanceCheckV1(command="ruff check src", exit_code=1, timed_out=False, passed=False, output_tail="E501 line too long"),
        AcceptanceCheckV1(command="pytest -q slow", exit_code=0, timed_out=True, passed=False, output_tail=""),
    ))
    text = render_resume_prompt(report)
    assert "pytest -q a" not in text
    assert "`ruff check src` exited 1" in text
    assert "E501 line too long" in text
    assert "`pytest -q slow` timed out" in text
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_goal_prompt.py`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Vorlagen anlegen** — `agenten/coding_agent/prompts/claude_goal_v1.md`:

```markdown
You are working in a sandboxed git repository at $workdir. Complete this goal.

## Goal
$objective

## Acceptance commands (every one must exit 0)
$acceptance

## Constraints
$constraints

## You may only change these paths
$allowed_paths

Work until every acceptance command passes. Run the acceptance commands yourself to check your work.
Do not modify tests or acceptance commands unless the goal explicitly requires it.
Do not use the network except for the model endpoint you were configured with.
When you are finished, reply with a short summary of what you changed.
```

`agenten/coding_agent/prompts/claude_resume_v1.md`:

```markdown
The acceptance commands did not all pass after your last round.

## Failing checks
$failing

Continue working on the same goal until every acceptance command passes.
Do not modify tests or acceptance commands unless the goal explicitly requires it.
```

- [ ] **Step 4: Renderer** — `agenten/coding_agent/goal_prompt.py`:

```python
"""Render Claude Code prompts from goal contracts using versioned templates."""
from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template

from agenten.coding_agent.contracts import AcceptanceReportV1, AgentGoalV1

_PROMPTS = Path(__file__).with_name("prompts")


@cache
def _template(name: str) -> Template:
    return Template((_PROMPTS / name).read_text(encoding="utf-8"))


def _bullets(items: tuple[str, ...], *, code: bool) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- `{item}`" if code else f"- {item}" for item in items)


def render_goal_prompt(goal: AgentGoalV1, *, workdir: str) -> str:
    return _template("claude_goal_v1.md").substitute(
        workdir=workdir,
        objective=goal.objective,
        acceptance=_bullets(goal.acceptance_commands, code=True),
        constraints=_bullets(goal.constraints, code=False),
        allowed_paths=_bullets(goal.allowed_paths, code=True),
    )


def render_resume_prompt(report: AcceptanceReportV1) -> str:
    blocks: list[str] = []
    for check in report.failing():
        headline = (
            f"- `{check.command}` timed out"
            if check.timed_out
            else f"- `{check.command}` exited {check.exit_code}"
        )
        blocks.append(f"{headline}\n\n```\n{check.output_tail}\n```")
    return _template("claude_resume_v1.md").substitute(failing="\n\n".join(blocks))
```

Prüfen, dass die `.md`-Dateien mit dem Paket ausgeliefert werden: `git grep -n "package_data\|include_package_data\|\[tool.setuptools" pyproject.toml setup.cfg setup.py 2>/dev/null`. Gibt es keine Paketierung (Repo läuft aus dem Checkout), ist nichts zu tun.

- [ ] **Step 5: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_goal_prompt.py`
Expected: alle grün

- [ ] **Step 6: Commit**

```bash
git add agenten/coding_agent/prompts/claude_goal_v1.md agenten/coding_agent/prompts/claude_resume_v1.md agenten/coding_agent/goal_prompt.py tests/coding_agent/test_goal_prompt.py
git commit -m "feat(coding-agent): render versioned goal and resume prompts for Claude Code"
```

### Task 5: Goal-Schleife (`ClaudeGoalRunner`)

**Files:**
- Create: `agenten/coding_agent/goal_runner.py`
- Test: `tests/coding_agent/test_goal_runner.py`

**Interfaces:**
- Consumes: Task 1–4 (`AgentGoalV1`, `AgentGoalRoundV1`, `AgentGoalResultV1`, `path_allowed`, `summarize_claude_stream`, `SandboxPort`, `SandboxCommand`, `run_acceptance`, `changed_paths`, `render_goal_prompt`, `render_resume_prompt`)
- Produces:
  - `@dataclass(frozen=True) ClaudeRunnerSettings(relay_url: str, model: str, small_model: str, workdir: str = "/work", allowed_tools: tuple[str, ...] = ("Bash","Read","Edit","Write","Glob","Grep"), acceptance_timeout_seconds: int = 600)`
  - `class BudgetGuardPort(Protocol): def exhausted(self) -> bool`
  - `build_claude_shell(settings: ClaudeRunnerSettings, *, max_turns: int, resume_session: str | None) -> str`
  - `build_claude_env(settings: ClaudeRunnerSettings, run_token: str) -> dict[str, str]`
  - `class ClaudeGoalRunner(sandbox: SandboxPort, settings: ClaudeRunnerSettings, budget: BudgetGuardPort, clock: Callable[[], float] = time.monotonic)` mit `async run(goal: AgentGoalV1, *, workspace_image_id: str, run_token: str) -> AgentGoalResultV1`

- [ ] **Step 1: Failing tests**

```python
import json
from decimal import Decimal

import pytest

from agenten.coding_agent.contracts import AgentGoalV1
from agenten.coding_agent.goal_runner import (
    ClaudeGoalRunner,
    ClaudeRunnerSettings,
    build_claude_env,
    build_claude_shell,
)
from tests.coding_agent.fakes import FakeSandbox, ok

SETTINGS = ClaudeRunnerSettings(
    relay_url="https://relay.example/anthropic", model="nvidia/super", small_model="nvidia/nano"
)
TOKEN = "run-token-secret"


def _goal(**overrides):
    values = dict(
        goal_id="g-1", objective="make tests pass", acceptance_commands=("pytest -q",),
        allowed_paths=("src/",), max_rounds=3, max_turns_per_round=10,
        budget_usd=Decimal("1"), timeout_seconds=600,
    )
    values.update(overrides)
    return AgentGoalV1(**values)


def _stream(session="s-1", subtype="success"):
    records = [{"type": "system", "subtype": "init", "session_id": session}]
    if subtype is not None:
        records.append({"type": "result", "subtype": subtype, "is_error": subtype != "success",
                        "num_turns": 2, "session_id": session, "usage": {"input_tokens": 5, "output_tokens": 3}})
    return "\n".join(json.dumps(r) for r in records)


class _Budget:
    def __init__(self, exhausted=False):
        self.value = exhausted

    def exhausted(self):
        return self.value


class _Script:
    """Answers claude runs, git status and acceptance by command shape."""

    def __init__(self, acceptance_exit_codes, changed=" M src/app.py\n", claude_stdout=None, timed_out=False):
        self.acceptance = list(acceptance_exit_codes)
        self.changed = changed
        self.claude_stdout = claude_stdout or _stream()
        self.timed_out = timed_out
        self.claude_runs = 0

    def __call__(self, image, command):
        if command.shell.startswith("cd /work && claude "):
            self.claude_runs += 1
            return ok(self.claude_stdout, image=f"img-r{self.claude_runs}", timed_out=self.timed_out)
        if command.shell.startswith("git -C /work status"):
            return ok(self.changed, image="discard")
        return ok("tail", image="discard", exit_code=self.acceptance.pop(0))


def _runner(script, budget=None, clock=None):
    sandbox = FakeSandbox(script)
    runner = ClaudeGoalRunner(sandbox=sandbox, settings=SETTINGS, budget=budget or _Budget(),
                              clock=clock or (lambda: 0.0))
    return runner, sandbox


def test_shell_and_env_are_exact():
    shell = build_claude_shell(SETTINGS, max_turns=7, resume_session="s-9")
    assert shell == (
        "cd /work && claude -p --output-format stream-json --verbose --model nvidia/super "
        "--max-turns 7 --dangerously-skip-permissions --allowedTools Bash,Read,Edit,Write,Glob,Grep "
        "--setting-sources '' --strict-mcp-config --resume s-9"
    )
    assert build_claude_env(SETTINGS, TOKEN) == {
        "ANTHROPIC_BASE_URL": "https://relay.example/anthropic",
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_MODEL": "nvidia/super",
        "ANTHROPIC_SMALL_FAST_MODEL": "nvidia/nano",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }


@pytest.mark.parametrize("url", ["http://relay.example", "ftp://x", "https://"])
def test_settings_reject_unsafe_relay_urls(url):
    with pytest.raises(ValueError):
        ClaudeRunnerSettings(relay_url=url, model="m", small_model="n")


def test_settings_allow_local_http_for_development():
    ClaudeRunnerSettings(relay_url="http://127.0.0.1:8787", model="m", small_model="n")


@pytest.mark.asyncio
async def test_red_then_green_resumes_the_same_session():
    script = _Script(acceptance_exit_codes=[1, 0])
    runner, sandbox = _runner(script)

    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)

    assert result.status == "achieved"
    assert [r.round for r in result.rounds] == [1, 2]
    assert result.final_image_id == "img-r2"
    claude_calls = [c for _, c in sandbox.calls if " claude " in c.shell]
    assert "--resume" not in claude_calls[0].shell
    assert claude_calls[1].shell.endswith("--resume s-1")
    assert "make tests pass" in claude_calls[0].stdin
    assert "`pytest -q` exited 1" in claude_calls[1].stdin
    assert sandbox.calls[0][0] == "img-0"
    assert TOKEN not in result.model_dump_json()
    assert TOKEN not in repr(result)


@pytest.mark.asyncio
async def test_exhausts_after_max_rounds():
    runner, _ = _runner(_Script(acceptance_exit_codes=[1, 1]))
    result = await runner.run(_goal(max_rounds=2), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "exhausted"
    assert len(result.rounds) == 2


@pytest.mark.asyncio
async def test_changes_outside_allowed_paths_stop_without_acceptance():
    script = _Script(acceptance_exit_codes=[], changed=" M src/app.py\n M tests/test_app.py\n")
    runner, sandbox = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "policy_violation"
    assert result.rounds[0].policy_violations == ("tests/test_app.py",)
    assert result.rounds[0].acceptance is None
    assert "tests/test_app.py" in (result.failure_reason or "")


@pytest.mark.asyncio
async def test_stream_without_session_fails_without_further_rounds():
    script = _Script(acceptance_exit_codes=[], claude_stdout="relay error: unauthorized")
    runner, _ = _runner(script)
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "failed"
    assert len(result.rounds) == 1
    assert script.claude_runs == 1


@pytest.mark.asyncio
async def test_claude_timeout_ends_the_goal():
    runner, _ = _runner(_Script(acceptance_exit_codes=[], timed_out=True))
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "timeout"
    assert result.rounds[0].timed_out is True


@pytest.mark.asyncio
async def test_budget_exhausted_before_first_round():
    script = _Script(acceptance_exit_codes=[])
    runner, _ = _runner(script, budget=_Budget(exhausted=True))
    result = await runner.run(_goal(), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "budget_exhausted"
    assert result.rounds == ()
    assert result.final_image_id == "img-0"
    assert script.claude_runs == 0


@pytest.mark.asyncio
async def test_goal_timeout_stops_before_next_round():
    times = iter([0.0, 0.0, 700.0])
    runner, _ = _runner(_Script(acceptance_exit_codes=[1]), clock=lambda: next(times))
    result = await runner.run(_goal(timeout_seconds=600), workspace_image_id="img-0", run_token=TOKEN)
    assert result.status == "timeout"
    assert len(result.rounds) == 1
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_goal_runner.py`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implementieren** — `agenten/coding_agent/goal_runner.py`:

```python
"""Drive Claude Code CLI in a sandbox until a goal's acceptance commands pass."""
from __future__ import annotations

import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from agenten.coding_agent.acceptance import changed_paths, run_acceptance
from agenten.coding_agent.claude_stream import summarize_claude_stream
from agenten.coding_agent.contracts import (
    AcceptanceReportV1,
    AgentGoalResultV1,
    AgentGoalRoundV1,
    AgentGoalV1,
    GoalStatus,
    path_allowed,
)
from agenten.coding_agent.goal_prompt import render_goal_prompt, render_resume_prompt
from agenten.coding_agent.sandbox import SandboxCommand, SandboxPort

_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class BudgetGuardPort(Protocol):
    def exhausted(self) -> bool: ...


@dataclass(frozen=True)
class ClaudeRunnerSettings:
    relay_url: str
    model: str
    small_model: str
    workdir: str = "/work"
    allowed_tools: tuple[str, ...] = ("Bash", "Read", "Edit", "Write", "Glob", "Grep")
    acceptance_timeout_seconds: int = 600

    def __post_init__(self) -> None:
        parsed = urlsplit(self.relay_url)
        secure = parsed.scheme == "https" and bool(parsed.hostname)
        local = parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
        if not (secure or local) or parsed.username or parsed.password or parsed.query:
            raise ValueError("relay_url must be https (or http on localhost)")
        for value in (self.model, self.small_model):
            if _MODEL.fullmatch(value) is None:
                raise ValueError("model ids must be plain identifiers")
        if not self.workdir.startswith("/") or any(ch.isspace() for ch in self.workdir):
            raise ValueError("workdir must be an absolute path without whitespace")


def build_claude_shell(settings: ClaudeRunnerSettings, *, max_turns: int, resume_session: str | None) -> str:
    parts = [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--model", settings.model, "--max-turns", str(max_turns),
        "--dangerously-skip-permissions", "--allowedTools", ",".join(settings.allowed_tools),
        "--setting-sources", "", "--strict-mcp-config",
    ]
    if resume_session is not None:
        if _SESSION.fullmatch(resume_session) is None:
            raise ValueError("session id is not a plain identifier")
        parts += ["--resume", resume_session]
    return f"cd {shlex.quote(settings.workdir)} && " + " ".join(shlex.quote(part) for part in parts)


def build_claude_env(settings: ClaudeRunnerSettings, run_token: str) -> dict[str, str]:
    return {
        "ANTHROPIC_BASE_URL": settings.relay_url,
        "ANTHROPIC_AUTH_TOKEN": run_token,
        "ANTHROPIC_MODEL": settings.model,
        "ANTHROPIC_SMALL_FAST_MODEL": settings.small_model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    }


class ClaudeGoalRunner:
    def __init__(
        self,
        *,
        sandbox: SandboxPort,
        settings: ClaudeRunnerSettings,
        budget: BudgetGuardPort,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sandbox = sandbox
        self._settings = settings
        self._budget = budget
        self._clock = clock

    async def run(self, goal: AgentGoalV1, *, workspace_image_id: str, run_token: str) -> AgentGoalResultV1:
        started = self._clock()
        image = workspace_image_id
        session: str | None = None
        report: AcceptanceReportV1 | None = None
        rounds: list[AgentGoalRoundV1] = []
        env = build_claude_env(self._settings, run_token)
        workdir = self._settings.workdir

        def finish(status: GoalStatus, reason: str | None) -> AgentGoalResultV1:
            return AgentGoalResultV1(
                goal_id=goal.goal_id, status=status, rounds=tuple(rounds),
                final_image_id=image, failure_reason=reason,
            )

        for number in range(1, goal.max_rounds + 1):
            if self._budget.exhausted():
                return finish("budget_exhausted", "provider budget exhausted before round")
            remaining = goal.timeout_seconds - (self._clock() - started)
            if remaining <= 0:
                return finish("timeout", "goal timeout reached")
            prompt = render_goal_prompt(goal, workdir=workdir) if report is None else render_resume_prompt(report)
            run = await self._sandbox.run(
                image,
                SandboxCommand(
                    shell=build_claude_shell(self._settings, max_turns=goal.max_turns_per_round, resume_session=session),
                    timeout_seconds=max(1, int(remaining)),
                    env=env,
                    stdin=prompt,
                ),
            )
            summary = summarize_claude_stream(run.stdout)
            session = summary.session_id or session
            input_image, image = image, run.output_image_id
            paths = await changed_paths(self._sandbox, image_id=image, workdir=workdir)
            violations = tuple(path for path in paths if not path_allowed(path, goal.allowed_paths))
            acceptance = None
            if not run.timed_out and not violations and summary.session_id is not None:
                acceptance = await run_acceptance(
                    self._sandbox, image_id=image, commands=goal.acceptance_commands,
                    workdir=workdir, timeout_seconds=self._settings.acceptance_timeout_seconds,
                )
            rounds.append(
                AgentGoalRoundV1(
                    round=number, input_image_id=input_image, output_image_id=image,
                    session_id=summary.session_id, result_subtype=summary.result_subtype,
                    num_turns=summary.num_turns, tool_call_count=summary.tool_call_count,
                    exit_code=run.exit_code, timed_out=run.timed_out,
                    journal_sha256=summary.journal_sha256, changed_paths=paths,
                    policy_violations=violations, acceptance=acceptance,
                )
            )
            if run.timed_out:
                return finish("timeout", "claude round timed out")
            if summary.session_id is None:
                return finish("failed", "claude stream carried no session")
            if violations:
                return finish("policy_violation", f"changed path outside allowed_paths: {violations[0]}"[:300])
            assert acceptance is not None
            if acceptance.passed:
                return finish("achieved", None)
            report = acceptance
        return finish("exhausted", "acceptance commands still failing after max_rounds")
```

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/`
Expected: alle grün

- [ ] **Step 5: Commit**

```bash
git add agenten/coding_agent/goal_runner.py tests/coding_agent/test_goal_runner.py
git commit -m "feat(coding-agent): drive Claude Code rounds until goal acceptance passes"
```

### Task 6: Tools `claude_code_build` und `sandbox_run_tests`

**Files:**
- Create: `agenten/coding_agent/tools.py`
- Test: `tests/coding_agent/test_tools.py`

**Interfaces:**
- Consumes: `ClaudeGoalRunner` (Task 5), `run_acceptance`, `SandboxPort` (Task 3), `AgentGoalV1`, `AgentGoalResultV1`, `AcceptanceReportV1` (Task 1)
- Produces:
  - `class WorkspaceCursor: image_id: str` (veränderlich, pro Team-Lauf geteilt)
  - `class CodingToolAuthorityPort(Protocol): async def authorize(self, tool_name: str, *, now: datetime) -> None` (wirft zum Verweigern)
  - `class RunTokenIssuerPort(Protocol): async def issue(self, goal_id: str) -> str`
  - `class GoalEvidenceSinkPort(Protocol): def record(self, result: AgentGoalResultV1) -> None`
  - `@dataclass(frozen=True) CodingToolLimits(max_calls: int, max_rounds: int, max_turns_per_round: int, budget_usd: Decimal, timeout_seconds: int)`
  - `ClaudeCodeBuildArgs`, `ClaudeCodeBuildResult`, `ClaudeCodeBuildTool(BaseTool)` mit Name `claude_code_build`
  - `SandboxRunTestsArgs`, `SandboxRunTestsTool(BaseTool)` mit Name `sandbox_run_tests`

- [ ] **Step 1: Failing tests**

```python
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from autogen_core import CancellationToken

from agenten.coding_agent.contracts import AgentGoalResultV1
from agenten.coding_agent.tools import (
    ClaudeCodeBuildArgs,
    ClaudeCodeBuildTool,
    CodingToolLimits,
    SandboxRunTestsArgs,
    SandboxRunTestsTool,
    WorkspaceCursor,
)
from tests.coding_agent.fakes import FakeSandbox, ok

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
LIMITS = CodingToolLimits(max_calls=2, max_rounds=3, max_turns_per_round=20, budget_usd=Decimal("2"), timeout_seconds=900)


class _Authority:
    def __init__(self, deny=False):
        self.deny = deny
        self.calls = []

    async def authorize(self, tool_name, *, now):
        self.calls.append(tool_name)
        if self.deny:
            raise PermissionError("grant revoked")


class _Tokens:
    async def issue(self, goal_id):
        return "tok-secret-" + goal_id


class _Sink:
    def __init__(self):
        self.results = []

    def record(self, result):
        self.results.append(result)


class _Runner:
    def __init__(self, status="achieved", final="img-new"):
        self.status = status
        self.final = final
        self.seen = []

    async def run(self, goal, *, workspace_image_id, run_token):
        self.seen.append((goal, workspace_image_id, run_token))
        return AgentGoalResultV1(goal_id=goal.goal_id, status=self.status, rounds=(), final_image_id=self.final,
                                 failure_reason=None if self.status == "achieved" else "nope")


def _build_tool(runner, authority=None, sink=None, cursor=None):
    return ClaudeCodeBuildTool(
        runner=runner, cursor=cursor or WorkspaceCursor("img-0"), authority=authority or _Authority(),
        tokens=_Tokens(), evidence=sink or _Sink(), limits=LIMITS, clock=lambda: NOW, goal_prefix="team-7",
    )


def _args():
    return ClaudeCodeBuildArgs(objective="add hello", acceptance_commands=["pytest -q"], allowed_paths=["src/"])


@pytest.mark.asyncio
async def test_build_tool_runs_goal_with_limits_and_advances_cursor():
    runner, sink, cursor = _Runner(), _Sink(), WorkspaceCursor("img-0")
    tool = _build_tool(runner, sink=sink, cursor=cursor)

    result = await tool.run(_args(), CancellationToken())

    goal, image, token = runner.seen[0]
    assert tool.name == "claude_code_build"
    assert goal.goal_id == "team-7-build-1"
    assert (goal.max_rounds, goal.max_turns_per_round, goal.timeout_seconds) == (3, 20, 900)
    assert goal.budget_usd == Decimal("2")
    assert image == "img-0"
    assert cursor.image_id == "img-new"
    assert result.status == "achieved"
    assert sink.results[0].goal_id == "team-7-build-1"
    assert "tok-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_policy_violation_does_not_advance_cursor():
    cursor = WorkspaceCursor("img-0")
    tool = _build_tool(_Runner(status="policy_violation", final="img-bad"), cursor=cursor)
    result = await tool.run(_args(), CancellationToken())
    assert cursor.image_id == "img-0"
    assert result.status == "policy_violation"


@pytest.mark.asyncio
async def test_denied_authority_runs_nothing_and_does_not_count():
    runner = _Runner()
    tool = _build_tool(runner, authority=_Authority(deny=True))
    with pytest.raises(PermissionError):
        await tool.run(_args(), CancellationToken())
    assert runner.seen == []


@pytest.mark.asyncio
async def test_call_ceiling_is_enforced():
    tool = _build_tool(_Runner())
    await tool.run(_args(), CancellationToken())
    await tool.run(_args(), CancellationToken())
    with pytest.raises(RuntimeError, match="ceiling"):
        await tool.run(_args(), CancellationToken())


def test_build_args_reject_multiline_commands():
    with pytest.raises(ValueError):
        ClaudeCodeBuildArgs(objective="x", acceptance_commands=["a\nb"])


@pytest.mark.asyncio
async def test_run_tests_tool_runs_on_cursor_image():
    sandbox = FakeSandbox(lambda image, command: ok("ok", exit_code=0))
    cursor = WorkspaceCursor("img-5")
    authority = _Authority()
    tool = SandboxRunTestsTool(sandbox=sandbox, cursor=cursor, authority=authority, clock=lambda: NOW,
                               workdir="/work", timeout_seconds=120, max_calls=1)

    report = await tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())

    assert tool.name == "sandbox_run_tests"
    assert report.passed is True
    assert sandbox.calls[0][0] == "img-5"
    assert authority.calls == ["sandbox_run_tests"]
    with pytest.raises(RuntimeError, match="ceiling"):
        await tool.run(SandboxRunTestsArgs(commands=["pytest -q"]), CancellationToken())
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/test_tools.py`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implementieren** — `agenten/coding_agent/tools.py`:

```python
"""Captain-authorized AutoGen tools that let team agents use Claude Code in a sandbox."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from autogen_core import CancellationToken
from autogen_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agenten.coding_agent.acceptance import run_acceptance
from agenten.coding_agent.contracts import AcceptanceReportV1, AgentGoalResultV1, AgentGoalV1, GoalStatus
from agenten.coding_agent.sandbox import SandboxPort

_ADVANCING: frozenset[GoalStatus] = frozenset({"achieved", "exhausted"})


class WorkspaceCursor:
    """The current workspace image of one team run, shared by its coding tools."""

    def __init__(self, image_id: str) -> None:
        self.image_id = image_id


class CodingToolAuthorityPort(Protocol):
    async def authorize(self, tool_name: str, *, now: datetime) -> None: ...


class RunTokenIssuerPort(Protocol):
    async def issue(self, goal_id: str) -> str: ...


class GoalEvidenceSinkPort(Protocol):
    def record(self, result: AgentGoalResultV1) -> None: ...


class _GoalRunnerPort(Protocol):
    async def run(self, goal: AgentGoalV1, *, workspace_image_id: str, run_token: str) -> AgentGoalResultV1: ...


@dataclass(frozen=True)
class CodingToolLimits:
    max_calls: int
    max_rounds: int
    max_turns_per_round: int
    budget_usd: Decimal
    timeout_seconds: int


def _single_lines(values: list[str]) -> list[str]:
    for value in values:
        if not value.strip() or any(ch in value for ch in "\x00\r\n"):
            raise ValueError("commands must be non-empty single lines")
    return values


class ClaudeCodeBuildArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=8000, description="What to build or change.")
    acceptance_commands: list[str] = Field(min_length=1, max_length=20, description="Shell commands that must all exit 0.")
    constraints: list[str] = Field(default_factory=list, max_length=30)
    allowed_paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)

    @field_validator("acceptance_commands")
    @classmethod
    def _commands(cls, value: list[str]) -> list[str]:
        return _single_lines(value)


class ClaudeCodeBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: GoalStatus
    rounds: int
    final_image_id: str
    failing_checks: list[str]
    failure_reason: str | None


class ClaudeCodeBuildTool(BaseTool[ClaudeCodeBuildArgs, ClaudeCodeBuildResult]):
    TOOL_NAME = "claude_code_build"

    def __init__(
        self,
        *,
        runner: _GoalRunnerPort,
        cursor: WorkspaceCursor,
        authority: CodingToolAuthorityPort,
        tokens: RunTokenIssuerPort,
        evidence: GoalEvidenceSinkPort,
        limits: CodingToolLimits,
        clock: Callable[[], datetime],
        goal_prefix: str,
    ) -> None:
        super().__init__(
            ClaudeCodeBuildArgs,
            ClaudeCodeBuildResult,
            self.TOOL_NAME,
            "Build or change code in the sandboxed workspace with Claude Code until the acceptance commands pass.",
        )
        self._runner = runner
        self._cursor = cursor
        self._authority = authority
        self._tokens = tokens
        self._evidence = evidence
        self._limits = limits
        self._clock = clock
        self._goal_prefix = goal_prefix
        self._calls = 0

    async def run(self, args: ClaudeCodeBuildArgs, cancellation_token: CancellationToken) -> ClaudeCodeBuildResult:
        if self._calls >= self._limits.max_calls:
            raise RuntimeError("claude_code_build call ceiling reached")
        await self._authority.authorize(self.TOOL_NAME, now=self._clock())
        self._calls += 1
        goal = AgentGoalV1(
            goal_id=f"{self._goal_prefix}-build-{self._calls}",
            objective=args.objective,
            acceptance_commands=tuple(args.acceptance_commands),
            constraints=tuple(args.constraints),
            allowed_paths=tuple(args.allowed_paths),
            max_rounds=self._limits.max_rounds,
            max_turns_per_round=self._limits.max_turns_per_round,
            budget_usd=self._limits.budget_usd,
            timeout_seconds=self._limits.timeout_seconds,
        )
        token = await self._tokens.issue(goal.goal_id)
        result = await self._runner.run(goal, workspace_image_id=self._cursor.image_id, run_token=token)
        self._evidence.record(result)
        if result.status in _ADVANCING:
            self._cursor.image_id = result.final_image_id
        last = result.rounds[-1].acceptance if result.rounds else None
        return ClaudeCodeBuildResult(
            status=result.status,
            rounds=len(result.rounds),
            final_image_id=result.final_image_id,
            failing_checks=[check.command for check in last.failing()] if last is not None else [],
            failure_reason=result.failure_reason,
        )


class SandboxRunTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: list[str] = Field(min_length=1, max_length=20, description="Shell commands to run in the workspace.")

    @field_validator("commands")
    @classmethod
    def _commands(cls, value: list[str]) -> list[str]:
        return _single_lines(value)


class SandboxRunTestsTool(BaseTool[SandboxRunTestsArgs, AcceptanceReportV1]):
    TOOL_NAME = "sandbox_run_tests"

    def __init__(
        self,
        *,
        sandbox: SandboxPort,
        cursor: WorkspaceCursor,
        authority: CodingToolAuthorityPort,
        clock: Callable[[], datetime],
        workdir: str,
        timeout_seconds: int,
        max_calls: int,
    ) -> None:
        super().__init__(
            SandboxRunTestsArgs,
            AcceptanceReportV1,
            self.TOOL_NAME,
            "Run test commands against the current sandboxed workspace without changing it.",
        )
        self._sandbox = sandbox
        self._cursor = cursor
        self._authority = authority
        self._clock = clock
        self._workdir = workdir
        self._timeout_seconds = timeout_seconds
        self._max_calls = max_calls
        self._calls = 0

    async def run(self, args: SandboxRunTestsArgs, cancellation_token: CancellationToken) -> AcceptanceReportV1:
        if self._calls >= self._max_calls:
            raise RuntimeError("sandbox_run_tests call ceiling reached")
        await self._authority.authorize(self.TOOL_NAME, now=self._clock())
        self._calls += 1
        return await run_acceptance(
            self._sandbox,
            image_id=self._cursor.image_id,
            commands=args.commands,
            workdir=self._workdir,
            timeout_seconds=self._timeout_seconds,
        )
```

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/coding_agent/`
Expected: alle grün

- [ ] **Step 5: Commit**

```bash
git add agenten/coding_agent/tools.py tests/coding_agent/test_tools.py
git commit -m "feat(coding-agent): expose Claude Code and sandbox tests as Captain-authorized tools"
```

### Task 7: Relay — Anfrage Anthropic → Chat

**Files:**
- Create: `agenten/provider_proxy/anthropic_bridge.py`
- Test: `tests/provider_proxy/test_anthropic_bridge_request.py` (Ordner `tests/provider_proxy/` mit `__init__.py` anlegen, falls nicht vorhanden)

**Interfaces:**
- Produces: `class BridgeError(ValueError)`, `class UpstreamFormatError(RuntimeError)`, `MAX_TOOL_NAME = 64`, `short_tool_name(name: str) -> str`, `@dataclass(frozen=True) ChatTranslation(payload: dict[str, object], tool_names: Mapping[str, str])` (Chat-Name → Original), `anthropic_request_to_chat(request: Mapping[str, object], *, allowed_models: frozenset[str]) -> ChatTranslation`

- [ ] **Step 1: Failing tests**

```python
import json

import pytest

from agenten.provider_proxy.anthropic_bridge import (
    BridgeError,
    anthropic_request_to_chat,
    short_tool_name,
)

MODELS = frozenset({"nvidia/super", "nvidia/nano"})
LONG = "mcp__captain_artifacts__write_artifact_with_a_really_long_descriptive_name_v2"


def _request(**overrides):
    request = {
        "model": "nvidia/super",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "You are Claude Code."}, {"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": "Create hello.py"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "x"},
                {"type": "text", "text": "Writing it."},
                {"type": "tool_use", "id": "toolu_1", "name": "Write", "input": {"file_path": "hello.py", "content": "print('hi')"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "File written"}]},
                {"type": "text", "text": "Now run it."},
            ]},
        ],
        "tools": [{"name": "Write", "description": "Write a file", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}],
        "tool_choice": {"type": "auto"},
        "stream": True,
        "temperature": 0.2,
        "stop_sequences": ["</done>"],
        "metadata": {"user_id": "u"},
        "thinking": {"type": "enabled", "budget_tokens": 1000},
        "output_config": {"effort": "high"},
    }
    request.update(overrides)
    return request


def test_full_request_translation():
    out = anthropic_request_to_chat(_request(), allowed_models=MODELS)
    p = out.payload
    assert p["model"] == "nvidia/super"
    assert p["max_tokens"] == 1024
    assert p["temperature"] == 0.2
    assert p["stop"] == ["</done>"]
    assert p["stream"] is True
    assert p["stream_options"] == {"include_usage": True}
    assert p["tool_choice"] == "auto"
    for dropped in ("metadata", "thinking", "output_config", "system", "stop_sequences"):
        assert dropped not in p
    messages = p["messages"]
    assert messages[0] == {"role": "system", "content": "You are Claude Code.\n\nBe brief."}
    assert messages[1] == {"role": "user", "content": "Create hello.py"}
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == "Writing it."
    call = messages[2]["tool_calls"][0]
    assert call["id"] == "toolu_1"
    assert call["function"]["name"] == "Write"
    assert json.loads(call["function"]["arguments"]) == {"file_path": "hello.py", "content": "print('hi')"}
    assert messages[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "File written"}
    assert messages[4] == {"role": "user", "content": "Now run it."}
    assert p["tools"][0] == {"type": "function", "function": {"name": "Write", "description": "Write a file",
                             "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}}}}


def test_unknown_model_is_rejected():
    with pytest.raises(BridgeError, match="allowlisted"):
        anthropic_request_to_chat(_request(model="claude-opus-4"), allowed_models=MODELS)


def test_images_are_rejected():
    req = _request(messages=[{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]}])
    with pytest.raises(BridgeError, match="image"):
        anthropic_request_to_chat(req, allowed_models=MODELS)


def test_long_tool_names_are_shortened_and_mapped_back():
    req = _request(
        tools=[{"name": LONG, "description": "", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": "go"}],
        tool_choice={"type": "tool", "name": LONG},
    )
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    short = out.payload["tools"][0]["function"]["name"]
    assert len(short) <= 64
    assert short == short_tool_name(LONG)
    assert out.tool_names[short] == LONG
    assert out.payload["tool_choice"] == {"type": "function", "function": {"name": short}}


@pytest.mark.parametrize("choice,expected", [({"type": "any"}, "required"), ({"type": "none"}, "none")])
def test_tool_choice_mapping(choice, expected):
    out = anthropic_request_to_chat(_request(tool_choice=choice), allowed_models=MODELS)
    assert out.payload["tool_choice"] == expected


def test_error_tool_result_is_prefixed():
    req = _request(messages=[
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True}]},
    ])
    out = anthropic_request_to_chat(req, allowed_models=MODELS)
    assert out.payload["messages"][-1] == {"role": "tool", "tool_call_id": "t1", "content": "Error: boom"}


@pytest.mark.parametrize("bad", [{"max_tokens": 0}, {"max_tokens": True}, {"messages": []}])
def test_invalid_shapes_are_bridge_errors(bad):
    with pytest.raises(BridgeError):
        anthropic_request_to_chat(_request(**bad), allowed_models=MODELS)


def test_non_stream_request_has_no_stream_options():
    out = anthropic_request_to_chat(_request(stream=False), allowed_models=MODELS)
    assert "stream" not in out.payload and "stream_options" not in out.payload
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/provider_proxy/test_anthropic_bridge_request.py`
Expected: FAIL (`ModuleNotFoundError` bzw. `ImportError`)

- [ ] **Step 3: Implementieren** — `agenten/provider_proxy/anthropic_bridge.py` (Request-Teil; Task 8 ergänzt dieselbe Datei):

```python
"""Pure translation between the Anthropic Messages API and OpenAI-style chat completions.

Claude Code speaks the Messages API; Nebius Token Factory serves chat
completions. Nothing here performs I/O. Unsupported Anthropic-only fields
(thinking, output_config, metadata, cache_control, betas) are dropped.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MAX_TOOL_NAME = 64


class BridgeError(ValueError):
    """The client request cannot be translated (maps to invalid_request_error)."""


class UpstreamFormatError(RuntimeError):
    """The upstream chat response is malformed (maps to api_error)."""


def short_tool_name(name: str) -> str:
    if len(name) <= MAX_TOOL_NAME:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: MAX_TOOL_NAME - 9]}_{digest}"


@dataclass(frozen=True)
class ChatTranslation:
    payload: dict[str, object]
    tool_names: Mapping[str, str]


def _require_str(block: Mapping[str, Any], key: str) -> str:
    value = block.get(key)
    if not isinstance(value, str):
        raise BridgeError(f"block field {key!r} must be a string")
    return value


def _system_text(system: object) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if not isinstance(system, list):
        raise BridgeError("system must be a string or a list of text blocks")
    parts = []
    for block in system:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise BridgeError("system blocks must be text")
        parts.append(_require_str(block, "text"))
    return "\n\n".join(parts)


def _tool_result_text(block: Mapping[str, Any]) -> str:
    content = block.get("content")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                raise BridgeError("tool_result content must be text (image blocks are not supported)")
            parts.append(_require_str(item, "text"))
        text = "\n".join(parts)
    else:
        raise BridgeError("tool_result content must be a string or a list")
    return f"Error: {text}" if block.get("is_error") else text


def _translate_message(message: object, chat_name: Callable[[str], str]) -> list[dict[str, object]]:
    if not isinstance(message, dict):
        raise BridgeError("each message must be an object")
    role = message.get("role")
    content = message.get("content")
    if role not in ("user", "assistant"):
        raise BridgeError("message role must be user or assistant")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        raise BridgeError("message content must be a string or a list")
    if role == "assistant":
        texts: list[str] = []
        calls: list[dict[str, object]] = []
        for block in content:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                texts.append(_require_str(block, "text"))
            elif kind == "tool_use":
                calls.append({
                    "id": _require_str(block, "id"),
                    "type": "function",
                    "function": {
                        "name": chat_name(_require_str(block, "name")),
                        "arguments": json.dumps(block.get("input") or {}, separators=(",", ":")),
                    },
                })
            elif kind in ("thinking", "redacted_thinking"):
                continue
            else:
                raise BridgeError(f"unsupported assistant block: {kind}")
        translated: dict[str, object] = {"role": "assistant", "content": "".join(texts) or None}
        if calls:
            translated["tool_calls"] = calls
        return [translated]
    tool_messages: list[dict[str, object]] = []
    texts = []
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "tool_result":
            tool_messages.append({
                "role": "tool",
                "tool_call_id": _require_str(block, "tool_use_id"),
                "content": _tool_result_text(block),
            })
        elif kind == "text":
            texts.append(_require_str(block, "text"))
        elif kind in ("image", "document"):
            raise BridgeError("image and document blocks are not supported")
        else:
            raise BridgeError(f"unsupported user block: {kind}")
    if texts:
        tool_messages.append({"role": "user", "content": "\n\n".join(texts)})
    return tool_messages


def _translate_tool(tool: object, chat_name: Callable[[str], str]) -> dict[str, object]:
    if not isinstance(tool, dict) or "input_schema" not in tool:
        raise BridgeError("only custom tools with input_schema are supported")
    return {
        "type": "function",
        "function": {
            "name": chat_name(_require_str(tool, "name")),
            "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


def _translate_tool_choice(choice: object, chat_name: Callable[[str], str]) -> object:
    if not isinstance(choice, dict):
        raise BridgeError("tool_choice must be an object")
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "function": {"name": chat_name(_require_str(choice, "name"))}}
    raise BridgeError(f"unsupported tool_choice: {kind}")


def anthropic_request_to_chat(request: Mapping[str, object], *, allowed_models: frozenset[str]) -> ChatTranslation:
    model = request.get("model")
    if not isinstance(model, str) or model not in allowed_models:
        raise BridgeError(f"model is not allowlisted: {model!r}")
    max_tokens = request.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise BridgeError("max_tokens must be a positive integer")
    raw_messages = request.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise BridgeError("messages must be a non-empty list")

    names: dict[str, str] = {}

    def chat_name(original: str) -> str:
        short = short_tool_name(original)
        names[short] = original
        return short

    messages: list[dict[str, object]] = []
    system = _system_text(request.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for message in raw_messages:
        messages.extend(_translate_message(message, chat_name))

    payload: dict[str, object] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    tools = request.get("tools")
    if tools:
        if not isinstance(tools, list):
            raise BridgeError("tools must be a list")
        payload["tools"] = [_translate_tool(tool, chat_name) for tool in tools]
        if request.get("tool_choice") is not None:
            payload["tool_choice"] = _translate_tool_choice(request.get("tool_choice"), chat_name)
    for key in ("temperature", "top_p"):
        if key in request:
            payload[key] = request[key]
    stops = request.get("stop_sequences")
    if stops:
        payload["stop"] = list(stops)  # type: ignore[call-overload]
    if request.get("stream") is True:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return ChatTranslation(payload=payload, tool_names=dict(names))
```

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/provider_proxy/test_anthropic_bridge_request.py`
Expected: alle grün

- [ ] **Step 5: Commit**

```bash
git add agenten/provider_proxy/anthropic_bridge.py tests/provider_proxy/
git commit -m "feat(proxy): translate Anthropic Messages requests into chat completions"
```

### Task 8: Relay — Antwort Chat → Anthropic (Stream und einmalig)

**Files:**
- Modify: `agenten/provider_proxy/anthropic_bridge.py` (ans Ende anhängen)
- Test: `tests/provider_proxy/test_anthropic_bridge_response.py`

**Interfaces:**
- Consumes: `UpstreamFormatError`, Imports aus Task 7
- Produces:
  - `STOP_REASONS: Mapping[str, str]`
  - `class ChatStreamToAnthropic(model: str, tool_names: Mapping[str, str])` mit `feed(chunk: Mapping[str, object]) -> list[dict[str, object]]`, `finish() -> list[dict[str, object]]`, Attributen `input_tokens: int`, `output_tokens: int`
  - `to_sse(event: Mapping[str, object]) -> bytes`
  - `chat_completion_to_anthropic(response: Mapping[str, object], *, model: str, tool_names: Mapping[str, str]) -> dict[str, object]`

- [ ] **Step 1: Failing tests**

```python
import json

import pytest

from agenten.provider_proxy.anthropic_bridge import (
    ChatStreamToAnthropic,
    UpstreamFormatError,
    chat_completion_to_anthropic,
    short_tool_name,
    to_sse,
)

LONG = "mcp__captain_artifacts__write_artifact_with_a_really_long_descriptive_name_v2"


def _chunk(delta=None, finish=None, usage=None):
    chunk = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        chunk["usage"] = usage
        if delta is None and finish is None:
            chunk["choices"] = []
    return chunk


def _run(chunks, tool_names=None):
    translator = ChatStreamToAnthropic(model="nvidia/super", tool_names=tool_names or {})
    events = []
    for chunk in chunks:
        events += translator.feed(chunk)
    events += translator.finish()
    return translator, events


def test_text_stream_produces_well_formed_event_sequence():
    translator, events = _run([
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "Hel"}),
        _chunk({"reasoning_content": "secret thoughts"}),
        _chunk({"content": "lo"}),
        _chunk(finish="stop"),
        _chunk(usage={"prompt_tokens": 11, "completion_tokens": 4}),
    ])
    assert [e["type"] for e in events] == [
        "message_start", "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert events[1]["content_block"] == {"type": "text", "text": ""}
    assert "".join(e["delta"]["text"] for e in events if e["type"] == "content_block_delta") == "Hello"
    assert events[-2]["delta"]["stop_reason"] == "end_turn"
    assert events[-2]["usage"] == {"input_tokens": 11, "output_tokens": 4}
    assert (translator.input_tokens, translator.output_tokens) == (11, 4)
    assert "secret thoughts" not in json.dumps(events)


def test_fragmented_tool_call_becomes_tool_use_with_input_json_deltas():
    short = short_tool_name(LONG)
    _, events = _run([
        _chunk({"content": "Let me write."}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": short, "arguments": ""}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"path":'}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}),
        _chunk(finish="tool_calls"),
    ], tool_names={short: LONG})
    starts = [e for e in events if e["type"] == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "text"
    assert starts[1]["index"] == 1
    assert starts[1]["content_block"] == {"type": "tool_use", "id": "call_1", "name": LONG, "input": {}}
    partial = "".join(e["delta"]["partial_json"] for e in events
                      if e["type"] == "content_block_delta" and e["delta"]["type"] == "input_json_delta")
    assert json.loads(partial) == {"path": "a.py"}
    stops = [e["index"] for e in events if e["type"] == "content_block_stop"]
    assert stops == [0, 1]
    assert events[-2]["delta"]["stop_reason"] == "tool_use"


def test_two_tool_calls_get_separate_blocks():
    _, events = _run([
        _chunk({"tool_calls": [{"index": 0, "id": "a", "function": {"name": "Read", "arguments": "{}"}}]}),
        _chunk({"tool_calls": [{"index": 1, "id": "b", "function": {"name": "Bash", "arguments": "{}"}}]}),
        _chunk(finish="tool_calls"),
    ])
    names = [e["content_block"]["name"] for e in events if e["type"] == "content_block_start"]
    assert names == ["Read", "Bash"]


def test_length_maps_to_max_tokens():
    _, events = _run([_chunk({"content": "x"}), _chunk(finish="length")])
    assert events[-2]["delta"]["stop_reason"] == "max_tokens"


def test_feed_after_finish_is_an_upstream_error():
    translator = ChatStreamToAnthropic(model="m", tool_names={})
    translator.finish()
    with pytest.raises(UpstreamFormatError):
        translator.feed(_chunk({"content": "late"}))


def test_sse_framing():
    assert to_sse({"type": "message_stop"}) == b'event: message_stop\ndata: {"type":"message_stop"}\n\n'


def test_non_stream_completion_translation():
    short = short_tool_name(LONG)
    message = chat_completion_to_anthropic({
        "choices": [{"message": {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": short, "arguments": '{"x":1}'}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2},
    }, model="nvidia/super", tool_names={short: LONG})
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["model"] == "nvidia/super"
    assert message["content"] == [
        {"type": "text", "text": "ok"},
        {"type": "tool_use", "id": "c1", "name": LONG, "input": {"x": 1}},
    ]
    assert message["stop_reason"] == "tool_use"
    assert message["usage"] == {"input_tokens": 7, "output_tokens": 2}


def test_non_stream_invalid_tool_arguments_are_upstream_errors():
    with pytest.raises(UpstreamFormatError):
        chat_completion_to_anthropic({"choices": [{"message": {"tool_calls": [
            {"id": "c", "function": {"name": "Bash", "arguments": "{broken"}}]}, "finish_reason": "tool_calls"}]},
            model="m", tool_names={})
```

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/provider_proxy/test_anthropic_bridge_response.py`
Expected: FAIL (`ImportError: cannot import name 'ChatStreamToAnthropic'`)

- [ ] **Step 3: Implementieren** — an `agenten/provider_proxy/anthropic_bridge.py` anhängen:

```python
STOP_REASONS: Mapping[str, str] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def to_sse(event: Mapping[str, object]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode("utf-8")


class ChatStreamToAnthropic:
    """Turn chat-completion stream chunks into Anthropic Messages stream events."""

    def __init__(self, *, model: str, tool_names: Mapping[str, str]) -> None:
        self._model = model
        self._names = dict(tool_names)
        self._message_id = "msg_" + uuid.uuid4().hex
        self._started = False
        self._finished = False
        self._next_index = 0
        self._open_index: int | None = None
        self._open_kind: str | None = None  # "text" or f"tool:{chat_index}"
        self._stop_reason = "end_turn"
        self.input_tokens = 0
        self.output_tokens = 0

    def _start(self) -> list[dict[str, object]]:
        if self._started:
            return []
        self._started = True
        return [{
            "type": "message_start",
            "message": {
                "id": self._message_id, "type": "message", "role": "assistant", "model": self._model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }]

    def _close(self) -> list[dict[str, object]]:
        if self._open_index is None:
            return []
        event: dict[str, object] = {"type": "content_block_stop", "index": self._open_index}
        self._open_index = None
        self._open_kind = None
        return [event]

    def _open(self, kind: str, block: dict[str, object]) -> list[dict[str, object]]:
        events = self._close()
        self._open_index = self._next_index
        self._open_kind = kind
        self._next_index += 1
        events.append({"type": "content_block_start", "index": self._open_index, "content_block": block})
        return events

    def feed(self, chunk: Mapping[str, object]) -> list[dict[str, object]]:
        if self._finished:
            raise UpstreamFormatError("stream chunk after finish")
        events = self._start()
        usage = chunk.get("usage")
        if isinstance(usage, Mapping):
            self.input_tokens = _count(usage.get("prompt_tokens"))
            self.output_tokens = _count(usage.get("completion_tokens"))
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise UpstreamFormatError("choices must be a list")
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise UpstreamFormatError("choice must be an object")
            delta = choice.get("delta") or {}
            if not isinstance(delta, Mapping):
                raise UpstreamFormatError("delta must be an object")
            text = delta.get("content")
            if isinstance(text, str) and text:
                if self._open_kind != "text":
                    events += self._open("text", {"type": "text", "text": ""})
                events.append({"type": "content_block_delta", "index": self._open_index,
                               "delta": {"type": "text_delta", "text": text}})
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, Mapping):
                    raise UpstreamFormatError("tool call must be an object")
                function = call.get("function") or {}
                if not isinstance(function, Mapping):
                    raise UpstreamFormatError("tool call function must be an object")
                kind = f"tool:{call.get('index', 0)}"
                if self._open_kind != kind:
                    name = function.get("name")
                    if not isinstance(name, str) or not name:
                        raise UpstreamFormatError("new tool call without a name")
                    events += self._open(kind, {
                        "type": "tool_use",
                        "id": call.get("id") or "toolu_" + uuid.uuid4().hex,
                        "name": self._names.get(name, name),
                        "input": {},
                    })
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    events.append({"type": "content_block_delta", "index": self._open_index,
                                   "delta": {"type": "input_json_delta", "partial_json": arguments}})
            reason = choice.get("finish_reason")
            if isinstance(reason, str):
                self._stop_reason = STOP_REASONS.get(reason, "end_turn")
        return events

    def finish(self) -> list[dict[str, object]]:
        if self._finished:
            return []
        events = self._start() + self._close()
        self._finished = True
        events.append({
            "type": "message_delta",
            "delta": {"stop_reason": self._stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
        })
        events.append({"type": "message_stop"})
        return events


def chat_completion_to_anthropic(
    response: Mapping[str, object], *, model: str, tool_names: Mapping[str, str]
) -> dict[str, object]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise UpstreamFormatError("response has no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    if not isinstance(message, Mapping):
        raise UpstreamFormatError("choice message must be an object")
    content: list[dict[str, object]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, Mapping) else None
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            raise UpstreamFormatError("tool call is malformed")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise UpstreamFormatError("tool call arguments are not JSON") from exc
        content.append({
            "type": "tool_use",
            "id": call.get("id") or "toolu_" + uuid.uuid4().hex,
            "name": tool_names.get(function["name"], function["name"]),
            "input": arguments if isinstance(arguments, dict) else {"value": arguments},
        })
    usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
    assert isinstance(usage, Mapping)
    reason = choice.get("finish_reason")
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": STOP_REASONS.get(reason, "end_turn") if isinstance(reason, str) else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": _count(usage.get("prompt_tokens")), "output_tokens": _count(usage.get("completion_tokens"))},
    }
```

- [ ] **Step 4: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/provider_proxy/`
Expected: alle grün

- [ ] **Step 5: Commit**

```bash
git add agenten/provider_proxy/anthropic_bridge.py tests/provider_proxy/test_anthropic_bridge_response.py
git commit -m "feat(proxy): translate chat completion streams back into Anthropic events"
```

### Task 9: Team-Schema `graph_flow`

**Files:**
- Modify: `agenten/agent_factory/candidate_evaluation.py` (Klassen `FactoryAutoGenTeamManifestV1` ~Zeile 101–199; neue Modelle davor)
- Create: `agenten/agent_factory/graph_flow_team.py`
- Modify: `agenten/agent_factory/team_execution.py` (Team-Konstruktion ~Zeile 1632–1652; `FactoryTeamRunResult.conversation_pattern` ~Zeile 876)
- Test: `tests/agent_factory/test_graph_flow_team.py`

**Interfaces:**
- Produces:
  - `class FactoryGraphEdgeV1(_FrozenModel): source: str; target: str; when: Literal["always", "approve", "iterate"] = "always"`
  - `class FactoryGraphSpecV1(_FrozenModel): entry: str; edges: tuple[FactoryGraphEdgeV1, ...]`
  - `FactoryAutoGenTeamManifestV1.conversation_pattern` erlaubt zusätzlich `"graph_flow"`; neues Feld `graph: FactoryGraphSpecV1 | None = None`
  - `verdict_condition(token: Literal["APPROVE", "ITERATE"]) -> Callable[[BaseChatMessage], bool]`
  - `build_graph_flow(*, graph: FactoryGraphSpecV1, participants: Sequence[ChatAgent], termination: TerminationCondition | None, max_turns: int) -> GraphFlow`

- [ ] **Step 1: Failing tests** — `tests/agent_factory/test_graph_flow_team.py`:

```python
import pytest
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination
from autogen_agentchat.messages import TextMessage
from pydantic import ValidationError

from agenten.agent_factory.candidate_evaluation import FactoryAutoGenTeamManifestV1
from agenten.agent_factory.graph_flow_team import build_graph_flow, verdict_condition
from agenten.llm.model_client import build_replay_model_client

AGENTS = ("architect", "builder", "tester", "reviewer", "reporter")
REF = {"uri": "artifact://sha256/" + "0" * 64, "sha256": "0" * 64}
LOOP_EDGES = [
    {"source": "architect", "target": "builder"},
    {"source": "builder", "target": "tester"},
    {"source": "tester", "target": "reviewer"},
    {"source": "reviewer", "target": "builder", "when": "iterate"},
    {"source": "reviewer", "target": "reporter", "when": "approve"},
]


def _manifest(**overrides):
    value = {
        "schema": "autogen-team.v1",
        "name": "build_loop",
        "conversation_pattern": "graph_flow",
        "graph": {"entry": "architect", "edges": LOOP_EDGES},
        "agents": [{"name": n, "system_prompt_ref": REF} for n in AGENTS],
        "memory_policy": "buffered",
        "max_messages": 30,
        "max_handoffs": 0,
        "max_tool_calls": 10,
        "termination_conditions": ["max_messages"],
        "entrypoint_command": ["python", "team.py"],
    }
    value.update(overrides)
    return FactoryAutoGenTeamManifestV1.model_validate(value, context={"allowed_tools": ()})
```

Vor dem Ausführen: das tatsächliche Format von `ArtifactRef` prüfen (`git grep -n "class ArtifactRef" -A15 agenten/agent_runtime/contracts.py`) und `REF` so anpassen, dass es valide ist (Ziel des Tests ist die Graph-Validierung, nicht die Referenz).

Weiter in derselben Datei:

```python
def test_graph_flow_manifest_validates():
    manifest = _manifest()
    assert manifest.conversation_pattern == "graph_flow"
    assert manifest.graph.entry == "architect"


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"graph": None}, "graph_flow requires a graph"),
        ({"conversation_pattern": "round_robin_group_chat"}, "graph is only valid with graph_flow"),
        ({"graph": {"entry": "ghost", "edges": LOOP_EDGES}}, "unknown graph node"),
        ({"graph": {"entry": "architect", "edges": LOOP_EDGES + [{"source": "builder", "target": "ghost"}]}}, "unknown graph node"),
        ({"graph": {"entry": "architect", "edges": LOOP_EDGES + [LOOP_EDGES[0]]}}, "duplicate graph edge"),
        ({"graph": {"entry": "architect", "edges": LOOP_EDGES[:3] + [{"source": "reviewer", "target": "builder", "when": "iterate"}, {"source": "reviewer", "target": "reporter"}]}}, "must not mix"),
        ({"agents": [{"name": n, "system_prompt_ref": REF} for n in AGENTS[:4]],
          "graph": {"entry": "architect", "edges": LOOP_EDGES[:3] + [{"source": "reviewer", "target": "builder"}]}}, "leaf"),
        ({"graph": {"entry": "builder", "edges": LOOP_EDGES}}, "unreachable"),
    ],
)
def test_invalid_graphs_are_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        _manifest(**overrides)


@pytest.mark.parametrize(
    "text,approve,iterate",
    [
        ("APPROVE", True, False),
        ("approve.\nall good", True, False),
        ("**ITERATE**\nfix the import", False, True),
        ("ITERATE: tests fail instead of APPROVE", False, False),
        ("I think we should APPROVE", False, False),
        ("", False, False),
    ],
)
def test_verdict_reads_only_the_first_line(text, approve, iterate):
    message = TextMessage(content=text, source="reviewer")
    assert verdict_condition("APPROVE")(message) is approve
    assert verdict_condition("ITERATE")(message) is iterate


def _agent(name, replies):
    return AssistantAgent(name, model_client=build_replay_model_client(list(replies)))


@pytest.mark.asyncio
async def test_iterate_loops_back_to_builder_then_approve_exits():
    manifest = _manifest()
    agents = [
        _agent("architect", ["plan: build hello"]),
        _agent("builder", ["built v1", "built v2"]),
        _agent("tester", ["2 failed", "all passed"]),
        _agent("reviewer", ["ITERATE\nfix failures", "APPROVE"]),
        _agent("reporter", ["summary"]),
    ]
    team = build_graph_flow(graph=manifest.graph, participants=agents,
                            termination=MaxMessageTermination(30), max_turns=30)

    result = await team.run(task="build hello")

    speakers = [m.source for m in result.messages if m.source != "user"]
    assert speakers == ["architect", "builder", "tester", "reviewer", "builder", "tester", "reviewer", "reporter"]
```

Hinweis zum letzten Test: `GraphFlow` kann zusätzlich ein internes Abschluss-Event melden; die Liste filtert deshalb auf Agent-Nachrichten. Falls `result.messages` Event-Typen ohne `source` eines Agenten enthält (z. B. `StopMessage` mit `source="DiGraphStopAgent"`), diese im Filter zusätzlich ausschließen — die erwartete Reihenfolge der fünf Agenten bleibt die Prüfung.

- [ ] **Step 2: Fehlschlag prüfen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/agent_factory/test_graph_flow_team.py`
Expected: FAIL (`ModuleNotFoundError: agenten.agent_factory.graph_flow_team` bzw. ValidationError für `graph_flow`)

- [ ] **Step 3: Manifest erweitern** — in `candidate_evaluation.py` vor `FactoryAutoGenTeamManifestV1` einfügen:

```python
class FactoryGraphEdgeV1(_FrozenModel):
    """One declarative edge; conditions are verdict tokens, never code."""

    source: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    target: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    when: Literal["always", "approve", "iterate"] = "always"


class FactoryGraphSpecV1(_FrozenModel):
    entry: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    edges: tuple[FactoryGraphEdgeV1, ...] = Field(min_length=1, max_length=50)
```

In `FactoryAutoGenTeamManifestV1`: `"graph_flow"` in die `conversation_pattern`-Literal-Liste aufnehmen und das Feld `graph: FactoryGraphSpecV1 | None = None` direkt nach `conversation_pattern` ergänzen. In `require_closed_topology` vor `return self` einfügen:

```python
        if self.conversation_pattern == "graph_flow":
            if self.graph is None:
                raise ValueError("graph_flow requires a graph")
            _validate_graph(self.graph, known_agents)
        elif self.graph is not None:
            raise ValueError("graph is only valid with graph_flow")
```

und als Modulfunktion (vor der Klasse):

```python
def _validate_graph(graph: "FactoryGraphSpecV1", agents: set[str]) -> None:
    nodes = {graph.entry} | {edge.source for edge in graph.edges} | {edge.target for edge in graph.edges}
    unknown = nodes - agents
    if unknown:
        raise ValueError(f"unknown graph node: {sorted(unknown)[0]}")
    keys = [(edge.source, edge.target, edge.when) for edge in graph.edges]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate graph edge")
    outgoing: dict[str, list[FactoryGraphEdgeV1]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.source, []).append(edge)
    for source, edges in outgoing.items():
        kinds = {edge.when == "always" for edge in edges}
        if len(kinds) > 1:
            raise ValueError(f"edges from {source} must not mix conditional and unconditional")
    if not agents - set(outgoing):
        raise ValueError("graph needs at least one leaf agent without outgoing edges")
    reachable, frontier = {graph.entry}, [graph.entry]
    while frontier:
        for edge in outgoing.get(frontier.pop(), ()):
            if edge.target not in reachable:
                reachable.add(edge.target)
                frontier.append(edge.target)
    if agents - reachable:
        raise ValueError(f"unreachable graph agent: {sorted(agents - reachable)[0]}")
```

(`FactoryGraphEdgeV1` muss oberhalb von `_validate_graph` definiert sein.)

- [ ] **Step 4: Builder-Modul** — `agenten/agent_factory/graph_flow_team.py`:

```python
"""Build an AutoGen GraphFlow from a declarative factory graph manifest."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Literal

from autogen_agentchat.base import ChatAgent, TerminationCondition
from autogen_agentchat.messages import BaseChatMessage
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow

from agenten.agent_factory.candidate_evaluation import FactoryGraphSpecV1

_TOKENS = {"approve": "APPROVE", "iterate": "ITERATE"}


def verdict_condition(token: Literal["APPROVE", "ITERATE"]) -> Callable[[BaseChatMessage], bool]:
    """Match only when the first line of the message is exactly the verdict token."""

    def condition(message: BaseChatMessage) -> bool:
        lines = message.to_text().strip().splitlines()
        return bool(lines) and lines[0].strip().strip("*`_.: ").upper() == token

    return condition


def build_graph_flow(
    *,
    graph: FactoryGraphSpecV1,
    participants: Sequence[ChatAgent],
    termination: TerminationCondition | None,
    max_turns: int,
) -> GraphFlow:
    by_name = {agent.name: agent for agent in participants}
    builder = DiGraphBuilder()
    for agent in participants:
        builder.add_node(agent)
    incoming = Counter(edge.target for edge in graph.edges)
    for edge in graph.edges:
        condition = None if edge.when == "always" else verdict_condition(_TOKENS[edge.when])  # type: ignore[arg-type]
        if incoming[edge.target] > 1:
            builder.add_edge(
                by_name[edge.source], by_name[edge.target], condition=condition,
                activation_group=f"{edge.source}->{edge.target}", activation_condition="any",
            )
        else:
            builder.add_edge(by_name[edge.source], by_name[edge.target], condition=condition)
    builder.set_entry_point(by_name[graph.entry])
    return GraphFlow(
        participants=builder.get_participants(),
        graph=builder.build(),
        termination_condition=termination,
        max_turns=max_turns,
    )
```

Hinweis: `verdict_condition` mit `"ITERATE: tests fail instead of APPROVE"` muss `False` liefern (erste Zeile ist nicht exakt das Token). Falls der Schleifen-Test mit dieser Aktivierungskonfiguration nicht die erwartete Reihenfolge liefert, die Aktivierungsparameter (`activation_group`/`activation_condition`) anhand der GraphFlow-Doku (`https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/graph-flow.html`, Abschnitt zu Schleifen und Aktivierungsgruppen) anpassen, bis der Test grün ist, und die Abweichung im Report festhalten. Den Test nicht abschwächen.

- [ ] **Step 5: In `team_execution.py` verdrahten**

1. Import: `from agenten.agent_factory.graph_flow_team import build_graph_flow` und `GraphFlow` in die bestehende `autogen_agentchat.teams`-Importzeile aufnehmen.
2. Typannotation `team: Swarm | SelectorGroupChat | RoundRobinGroupChat | None` um `GraphFlow` erweitern.
3. Nach dem `round_robin_group_chat`-Zweig:

```python
        elif manifest.conversation_pattern == "graph_flow":
            assert manifest.graph is not None
            team = build_graph_flow(
                graph=manifest.graph,
                participants=participants,
                termination=termination,
                max_turns=manifest.max_messages,
            )
```

4. `FactoryTeamRunResult.conversation_pattern` Literal um `"graph_flow"` erweitern.
5. Weitere Literal-Kopien suchen und ergänzen: `git grep -n '"round_robin_group_chat",' agenten gateway`. Jede Fundstelle, die die Muster aufzählt, bekommt `"graph_flow"`; Fundstellen, die bewusst eine Teilmenge prüfen (z. B. Live-Demo-Muster), unverändert lassen und im Report nennen.
6. Prüfen, dass die Teilnehmer in `team_execution` mit `name=agent.name` aus dem Manifest gebaut werden (`git grep -n "AssistantAgent(" agenten/agent_factory/team_execution.py`); falls nicht, im Report melden (BLOCKED), statt Namen umzubiegen.

- [ ] **Step 6: Tests laufen lassen**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest --no-cov -q -p no:cacheprovider tests/agent_factory/test_graph_flow_team.py`
Expected: alle grün

Danach die bestehenden Team-/Manifest-Tests: `git grep -ln "FactoryAutoGenTeamManifestV1\|conversation_pattern\|HostAutoGen" tests` und diese Dateien laufen lassen. Erwartet: grün bis auf die bekannten Altlasten aus den Global Constraints.

- [ ] **Step 7: Commit**

```bash
git add agenten/agent_factory/candidate_evaluation.py agenten/agent_factory/graph_flow_team.py agenten/agent_factory/team_execution.py tests/agent_factory/test_graph_flow_team.py
git commit -m "feat(factory): add declarative graph_flow team pattern with verdict loops"
```

### Task 10: Gate

**Files:** keine neuen

- [ ] **Step 1: Schnelle Gates**

Run:
```powershell
C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config gateway
C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov -p no:cacheprovider tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py tests/coding_agent tests/provider_proxy tests/agent_factory/test_graph_flow_team.py
C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe scripts/verify_submission.py
```
Expected: compileall ohne Ausgabe; alle Tests grün; `Submission evidence check passed.` Schlägt ein Import-Boundary-Test wegen des neuen Pakets `agenten/coding_agent` fehl, die Regel lesen und das Paket so einordnen, wie die Regel es verlangt (nicht die Regel aufweichen); bei Unklarheit BLOCKED melden.

- [ ] **Step 2: Volle Offline-Suite (einmal)**

Run: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -m "not live" -p no:cacheprovider --no-cov -q > <tmp außerhalb des Repos>\m1a-suite.log 2>&1` (dauert ~40 min; im Hintergrund laufen lassen und die Schlusszeile abwarten).
Expected: grün bis auf die bekannten Altlasten aus den Global Constraints.

- [ ] **Step 3: Aufräumen und Commit (nur falls Anpassungen nötig waren)**

`.coverage.*` löschen; nur falls im Gate Tests angepasst werden mussten:

```bash
git add <angepasste Testdateien>
git commit -m "test: align existing suites with coding-agent and graph_flow additions"
```

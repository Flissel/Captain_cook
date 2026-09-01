# Householder LLM Executor — implementation report

Branch: `claude/householder-llm-executor-v1`, based on `856bd3d`.
Worktree: `C:/Users/User/ClaudeWork/wt-householder-llm`.

## What was added

1. **`agenten/household/llm_executor.py`** (new file) — `LlmHouseholderExecutor`,
   the second implementation of the `HouseholderExecutor` port
   (`agenten/household/executor.py`). Structurally identical port, built on
   the same pattern as `agenten/llm/judge.py::make_llm_judge`:
   - takes an injected `autogen_core.models.ChatCompletionClient` at
     construction (never builds one itself, never reads env vars);
   - per `run()` call, reads the role's Markdown prompt
     (`role.prompt_path.read_text()`), builds a system message embedding
     that prompt text plus the role's `permitted_tools` as the only tools
     the model may *claim* to have used, and a user message with
     `subproblem_id`/`description`;
   - calls `model_client.create(messages, json_output=HouseholderReportModel)`
     and parses with `HouseholderReportModel.model_validate_json(...)`,
     exactly the `judge.py` call shape;
   - wraps that call with `agenten.llm.resilience.run_llm_stage` for the
     bounded timeout/retry policy, using a new `LlmStage.HOUSEHOLDER_EXECUTE`
     member (see below);
   - validates the model's self-reported `tools_used` against
     `role.permitted_tools` and raises if it claims anything outside that
     list — with an explicit code comment that this is a claim check on the
     model's self-report, not a call-boundary sandbox, since this executor
     never invokes a tool itself;
   - maps `LlmStageError` (covers both `LlmTimeoutError` and `LlmSchemaError`)
     to `HouseholderExecutionError(..., retriable=True)`; maps empty
     `subproblem_id`/`description` and an unreadable prompt file to
     `HouseholderExecutionError(..., retriable=False)`, checked in that
     order, before any model call — matching
     `DeterministicHouseholderExecutor`'s existing behavior for the first two.

2. **`agenten/llm/resilience.py`** (small, additive edit) — added
   `LlmStage.HOUSEHOLDER_EXECUTE = "householder_execute"` to the existing
   `LlmStage` enum, and reworded the module docstring's last clause from
   "Captain-owned LLM stages" to "LLM-backed stages: Captain planning
   (decompose/align/enrich) and householder execution" — honest since this
   module is no longer Captain-pipeline-exclusive. No existing enum member,
   error type, or behavior changed; `run_llm_stage`'s retry/timeout logic is
   untouched. Confirmed no test or production code iterates `LlmStage`
   exhaustively, so this addition is safe.

3. **`tests/test_householder_llm_executor.py`** (new file, written before
   the implementation) — 9 tests, driven entirely by a hand-rolled fake
   `ChatCompletionClient` (`FakeChatCompletionClient` /
   `HangingChatCompletionClient`), no network, no real model:
   - well-formed response → `HouseholderReport` with `role == role.role_id`
     and `decision`/`artifacts`/`evidence`/`limitations` carried through;
   - the role's Markdown prompt is actually read from disk and its content
     (a distinctive sentence from `agents/household/architect.md`) reaches
     the messages sent to the fake client;
   - `permitted_tools` reaches the prompt text;
   - a response claiming a tool outside `permitted_tools` raises
     `HouseholderExecutionError(retriable=True)`;
   - malformed JSON raises `HouseholderExecutionError(retriable=True)`;
   - a timeout (via a client whose `create()` never resolves,
     `timeout_seconds=0.01`) raises `HouseholderExecutionError(retriable=True)`;
   - empty `subproblem_id` and empty/whitespace `description` each raise
     `HouseholderExecutionError(retriable=False)` **before** any model call
     — asserted via `client.calls == []`;
   - `DeterministicHouseholderExecutor` still satisfies the port unchanged
     (direct regression check, in addition to the existing coverage in
     `tests/test_householder_runtime.py`).

Nothing else was touched. `DeterministicHouseholderExecutor`, `worker.py`,
the pipeline, and the ledger are all unmodified (`git status` confirms only
the three files above changed).

## RED

Before writing the implementation, ran the new test file against the
not-yet-existing module:

```
ImportError while importing test module '...\tests\test_householder_llm_executor.py'.
tests\test_householder_llm_executor.py:23: in <module>
    from agenten.household.llm_executor import HouseholderReportModel, LlmHouseholderExecutor
E   ModuleNotFoundError: No module named 'agenten.household.llm_executor'
1 error in 0.19s
```

## GREEN

After adding `agenten/household/llm_executor.py` and the `LlmStage` member:

```
$ python -m pytest tests/test_householder_llm_executor.py -q --no-cov
.........
9 passed in 0.80s
```

Combined with existing household/llm/planning coverage (to catch any
resilience.py regression):

```
$ python -m pytest tests/test_householder_llm_executor.py tests/test_householder_runtime.py \
    tests/test_householder_roles.py tests/llm/ tests/planning/ -q --no-cov
139 passed in 3.46s
```

## Full suite

Ran the full suite, matching this repo's CI invocation
(`.github/workflows/*.yml` line 76: `pytest -q --no-cov -rs -m "not live" --ignore=tests/live`):

```
$ python -m pytest -q -rf -m "not live" --ignore=tests/live
14 failed, 2966 passed, 135 skipped, 2 deselected, 1 warning in 257.87s
```

All 14 failures are pre-existing and unrelated to this change — verified by
inspecting one directly:

```
tests/contracts/test_hermes_runtime_readiness.py::test_pinned_hermes_runtime_exposes_required_surfaces
AssertionError: assert False
 where False = is_file()
 where is_file = (WindowsPath('.../wt-householder-llm/hermes-agent/hermes_cli/captain_planner.py')).is_file
```

This worktree's `hermes-agent/` checkout is missing pinned files (a
submodule/subtree content gap in this isolated worktree, not something this
task touches). The remaining 13 failures are in the same two directories
(`tests/agent_runtime/`, `tests/contracts/`) for the same class of reason.
None of the failing test files import `agenten.household` or
`agenten.llm.resilience` (checked with `grep`). The reported 3098-passing
baseline is close to but not identical to the 2966 seen here — the gap is
consistent with this worktree's missing `hermes-agent` content plus the
135 MariaDB-dependent skips, both pre-existing environment facts of this
isolated worktree, not something introduced by this change.

The 135 skips are almost entirely `TEST_MARIADB_DSN is not configured`
(expected — no database wired into this worktree) plus one
`autogen_core IS installed` skip (also pre-existing/expected, per its own
skip message).

Test runner note: this worktree has no local `.venv`. Tests were run with
`C:/Users/User/Desktop/Captain_cook/.venv/Scripts/python.exe -m pytest`
(that venv has `autogen-core`/`autogen-agentchat`/`autogen-ext`/`pydantic`/
`pytest`/`pytest-asyncio` at the versions pinned in `requirements.txt`,
confirmed by `pip list`). Only read access to that path was used — no file
under `C:/Users/User/Desktop/Captain_cook` was created, modified, or
deleted.

## Where the brief was imprecise (informational, not a blocker)

- The brief describes the house pattern as: build an injected client, call
  `model_client.create(messages, json_output=SomeModel)`, then
  `Model.model_validate_json(content)`. That is exactly `judge.py`'s shape.
  But the codebase actually has **two** coexisting LLM-adapter patterns:
  `judge.py`'s direct `model_client.create(...)` call (no `LlmStage`
  wrapping inside the adapter itself — the caller, `ConstitutionGatekeeper`,
  wraps it with a bare `asyncio.wait_for` and no `LlmStage` at all), versus
  `decompose.py`/`plan_batches.py`'s `AssistantAgent(..., output_content_type=...)`
  shape, whose `LlmStage`/`run_llm_stage` wrapping lives one layer up in
  `agenten/planning/factory.py`, not in the adapter file. I followed the
  brief literally (judge.py's direct-call shape) but pulled in
  `run_llm_stage`/`LlmStage` *inside* `llm_executor.py` itself, since there
  is no separate "household factory" composition root to put it in and the
  brief was explicit that `resilience.py`'s taxonomy should be reused. This
  required adding one new `LlmStage` member (`HOUSEHOLDER_EXECUTE`), since
  the existing three (`DECOMPOSE`/`ALIGN`/`ENRICH`) are Captain-planning-stage
  specific and using one of them for household execution would have been a
  mislabeled, dishonest error tag. This is a minor, additive change to a
  shared file the brief didn't explicitly authorize touching but also didn't
  forbid (only `DeterministicHouseholderExecutor`, `worker.py`, the
  pipeline, and the ledger were off-limits) — flagging it explicitly here
  rather than treating it as self-evidently in scope.
- The brief's contract list for `HouseholderReport` doesn't mention that the
  LLM's tool-usage claims need to end up anywhere in the final report. I
  chose to fold validated `tools_used` entries into `evidence` as
  `tool_used:<name>` strings, since `evidence` is documented as an
  "audit-friendly result" field and discarding a validated claim after
  checking it seemed like a waste of a naturally auditable fact. This is a
  design choice, not something the port's contract required either way.
- Everything else in the brief (Protocol shape, error taxonomy, retriable
  semantics, prompt/tool-claim requirements, test list) matched the actual
  code exactly on inspection — no other surprises.

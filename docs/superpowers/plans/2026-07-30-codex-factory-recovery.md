# Codex Factory Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve streamed Codex evidence and resume an interrupted Factory build in the same Captain attempt without recreating the suite or repeating Hermes work.

**Architecture:** A private append-only JSONL journal and typed terminal result make process evidence durable. A content-bound checkpoint store advances one build through scaffold, implementation, and seal, while a separate Captain runtime-retry authorization permits only a bounded continuation of the interrupted Codex seal replay.

**Tech Stack:** Python 3.11, Pydantic v2, asyncio subprocesses, PowerShell 7, pytest, Git detached worktrees.

## Global Constraints

- Do not run Hermes, Codex provider, benchmarks, Gateway promotion, or Minibook live projection in this goal.
- Preserve immutable v15-v18 evidence and every existing private candidate workspace.
- Never read, log, copy, or modify `OPENAI_API_KEY`.
- Captain remains the only retry and seal authority; Minibook remains a projection.
- Every behavior change follows red-green-refactor and receives a narrow Conventional Commit.
- Existing deterministic and non-live paths remain compatible.

---

### Task 1: Durable Codex JSONL and truthful terminal receipts

**Files:**
- Modify: `scripts/codex-session.ps1`
- Modify: `agenten/execution/codex_supervisor.py`
- Modify: `agenten/agent_factory/codex_build_execution.py`
- Modify: `gateway/agent_factory_live_operator.py`
- Test: `tests/execution/test_codex_session_recovery.py`
- Test: `tests/agent_factory/test_codex_build_execution.py`

**Interfaces:**
- `CodexRunTerminalStatus = Literal["succeeded", "failed", "timed_out", "cancelled"]`.
- `CodexRunResult` adds `terminal_status`, `journal_path`, and
  `journal_sha256`; `jsonl_lines` remains the sanitized snapshot consumed by
  existing callers.
- `PowerShellCodexRunner(..., journal_path: Path, ...)` owns a private journal
  and returns a result on every terminal path.
- `FactoryCodexRunnerFactory.__call__` accepts `journal_path`.

- [ ] **Step 1: Add failing PowerShell streaming and runner timeout tests**

Add tests that launch a fixture child which writes one JSONL object, flushes,
and then waits. Assert that the journal contains the line before termination.
Add zero-event and partial-event timeout assertions for exit `124`, status
`timed_out`, stable journal digest, and retained lines.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/execution/test_codex_session_recovery.py -k "stream or timeout"
```

Expected: failures because the runner has no durable journal or terminal
classification.

- [ ] **Step 3: Stream complete lines and fsync the private journal**

Change the PowerShell launcher to forward each complete stdout line immediately
while draining stderr concurrently. In `PowerShellCodexRunner`, read stdout
line-by-line, append `line + b"\n"` through a private binary handle, flush and
`os.fsync` after every line, and concurrently drain stderr. On timeout, cancel
the verified tree, wait for both readers, and build the terminal result from the
journal. Never include stderr or prompt text in the result.

- [ ] **Step 4: Add failing receipt classification tests**

Add direct `_session_receipt` tests asserting that `124` with zero or partial
events serializes `status == "timed_out"`, `exit_code == 124`, and the journal
digest/count. Assert that exit `0` with zero events still fails closed.

- [ ] **Step 5: Fix receipt ordering and propagate the journal path**

Classify the terminal outcome before successful-run JSONL validation. Pass a
deterministic journal path below `state_root / "journals"` through the live
runner factory. Keep receipt persistence write-once.

- [ ] **Step 6: Verify and commit Task 1**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/execution/test_codex_session_recovery.py tests/agent_factory/test_codex_build_execution.py
git add scripts/codex-session.ps1 agenten/execution/codex_supervisor.py agenten/agent_factory/codex_build_execution.py gateway/agent_factory_live_operator.py tests/execution/test_codex_session_recovery.py tests/agent_factory/test_codex_build_execution.py
git commit -m "fix: retain Factory Codex timeout evidence"
```

Expected: focused tests pass and the commit contains no live artifacts.

### Task 2: Monotonic scaffold, implementation, and seal checkpoints

**Files:**
- Create: `agenten/agent_factory/codex_build_recovery.py`
- Modify: `agenten/agent_factory/codex_build_execution.py`
- Modify: `gateway/agent_factory_live_operator.py`
- Create: `tests/agent_factory/test_codex_build_recovery.py`
- Modify: `tests/agent_factory/test_codex_build_execution.py`

**Interfaces:**
- `FactoryCodexBuildPhase = Literal["scaffold_ready", "implementation_running", "implementation_interrupted", "implementation_complete", "sealed"]`.
- Frozen `FactoryCodexBuildCheckpointV1` binds `job_id`, `correlation_id`,
  `attempt`, `invocation_id`, `workspace_ref`, `workspace_root`,
  `base_revision`, `brief_sha256`, `phase`, `resume_ordinal`, optional
  `terminal_receipt_sha256`, and `updated_at`.
- `FilesystemFactoryCodexBuildCheckpointStore.load(invocation)` returns a
  validated checkpoint or `None`; `advance(previous, next)` writes atomically
  and accepts only the declared monotonic transitions.
- `GitDetachedFactoryWorkspacePreparer.prepare_or_recover(...)` returns the
  same exact clean scaffold workspace when checkpoint bindings match.

- [ ] **Step 1: Write failing checkpoint transition and conflict tests**

Cover initial scaffold creation, running/interrupted/running/complete/sealed
transitions, identical replay, skipped/backward transitions, changed brief,
changed base revision, changed workspace, and missing workspace.

- [ ] **Step 2: Run checkpoint tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_codex_build_recovery.py
```

Expected: import failure because the checkpoint module does not exist.

- [ ] **Step 3: Implement the frozen contract and atomic store**

Use canonical sorted JSON, SHA-256 digests, write-once initial creation, and
atomic replacement guarded by exact previous bytes. Permit only:
`scaffold_ready -> implementation_running`, `implementation_running ->
implementation_interrupted|implementation_complete`,
`implementation_interrupted -> implementation_running`, and
`implementation_complete -> sealed`. An identical target is an idempotent
replay; every conflict raises `FactoryDispatchError`.

- [ ] **Step 4: Write failing executor recovery tests**

Assert the first call creates the detached workspace and `.captain-inputs` once,
records `scaffold_ready`, and starts implementation. Simulate timeout and assert
the checkpoint retains the same workspace and materialized input digests. A
second ordinary call must stop at `implementation_interrupted`; Task 3 supplies
the matching Captain authorization that permits the transition back to
`implementation_running`. Assert artifacts are checked only after
`implementation_complete`, and `sealed` is terminal/idempotent.

- [ ] **Step 5: Integrate phased execution**

Split `CodexCliFactoryBuildExecutor.execute` into private scaffold,
implementation, and seal methods. Record the terminal session receipt digest in
the interrupted/completed checkpoint. Expose a narrow authorized-resume hook
that Task 3 can call; it must reject use without the authorization decision that
Task 3 injects. On recovery, validate the existing worktree HEAD and every
materialized input digest rather than recreating them.

- [ ] **Step 6: Verify and commit Task 2**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_codex_build_recovery.py tests/agent_factory/test_codex_build_execution.py
git add agenten/agent_factory/codex_build_recovery.py agenten/agent_factory/codex_build_execution.py gateway/agent_factory_live_operator.py tests/agent_factory/test_codex_build_recovery.py tests/agent_factory/test_codex_build_execution.py
git commit -m "feat: checkpoint Factory Codex build phases"
```

Expected: recovery tests pass without provider calls.

### Task 3: Captain-authorized same-attempt runtime retry

**Files:**
- Modify: `agenten/agent_factory/skill_sequence.py`
- Modify: `agenten/agent_factory/orchestration.py`
- Modify: `agenten/agent_factory/hermes_cli.py`
- Modify: `agenten/agent_factory/codex_build_execution.py`
- Modify: `gateway/agent_factory_live_composition.py`
- Modify: `gateway/agent_factory_live_operator.py`
- Test: `tests/agent_factory/test_skill_sequence.py`
- Test: `tests/agent_factory/test_hermes_cli.py`
- Test: `tests/agent_factory/test_codex_build_execution.py`

**Interfaces:**
- Frozen Pydantic `FactoryRuntimeRetryAuthorizationV1` uses schema
  `captain.factory-runtime-retry-authorization.v1` and binds the interrupted
  seal invocation, checkpoint/receipt refs, same Factory attempt,
  `resume_ordinal` (`1..2`), `maximum_runtime_seconds`, `issued_at`, and
  `expires_at`.
- `FactoryDispatch.runtime_retry_authorization` defaults to `None`.
- `FactorySkillReplayRecord.state` adds `interrupted` and retains
  `failure_kind == "codex_runtime_interrupted"` plus the checkpoint and receipt
  artifact references.
- `FactorySkillReplayStore.interrupt(...)` and `resume(...)` are atomic;
  `resume` requires a validated authorization and returns an acquired claim.

- [ ] **Step 1: Write failing authorization validation tests**

Cover valid exact binding plus wrong producer/status, job, correlation,
invocation, attempt, checkpoint/receipt digest, ordinal, expiry, runtime bound,
and reuse.

- [ ] **Step 2: Run the contract tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_skill_sequence.py -k runtime_retry
```

Expected: import or attribute failures for the missing contract.

- [ ] **Step 3: Implement the distinct Captain authorization**

Add strict model validators and a single helper that validates it against the
current dispatch, invocation, interrupted checkpoint, terminal receipt, and
current UTC time. Do not reuse or weaken behavioral improvement authority.

- [ ] **Step 4: Write failing replay and Hermes call-count tests**

Simulate a typed Codex timeout from `seal_codex_build`. Assert the replay becomes
`interrupted`, discovery and brief stay completed, a normal redispatch fails,
and an authorized redispatch resumes only the seal. Assert Hermes subprocess
call count and suite identity do not change.

- [ ] **Step 5: Implement interrupted replay and typed recovery**

Introduce `FactoryCodexBuildInterrupted` carrying the private checkpoint and
terminal receipt refs. Catch only that type in `HermesCliFactory.dispatch`,
transition the acquired seal replay to `interrupted`, and re-raise. On the next
same invocation, validate and consume Captain runtime authorization before the
atomic `resume` transition. All other exceptions continue to become immutable
failed replays.

- [ ] **Step 6: Wire the production composition fail closed**

Default live composition must not mint authorization itself. It accepts only a
Captain-issued authorization supplied through the existing dispatch boundary;
absence or mismatch stops before a new Codex process.

- [ ] **Step 7: Verify and commit Task 3**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_skill_sequence.py tests/agent_factory/test_hermes_cli.py tests/agent_factory/test_codex_build_execution.py
git add agenten/agent_factory/skill_sequence.py agenten/agent_factory/orchestration.py agenten/agent_factory/hermes_cli.py agenten/agent_factory/codex_build_execution.py gateway/agent_factory_live_composition.py gateway/agent_factory_live_operator.py tests/agent_factory/test_skill_sequence.py tests/agent_factory/test_hermes_cli.py tests/agent_factory/test_codex_build_execution.py
git commit -m "feat: authorize Factory Codex runtime recovery"
```

Expected: same-attempt recovery passes and behavioral retry tests remain green.

### Task 4: Local gate and immutable v19 preparation

**Files:**
- Modify: `scripts/run-business-benchmark-demo.ps1`
- Modify only if its invocation contract changes: `scripts/run-business-benchmark-demo-secure.ps1`
- Modify: `tests/scripts/test_run_business_benchmark_demo.py`
- Modify: `docs/superpowers/plans/2026-07-28-business-benchmark-live-bootstrap-gaps.md`

**Interfaces:**
- The live operator reports `codex_build_interrupted` with redacted checkpoint,
  receipt, and required Captain authorization fields.
- A dry-run/preflight path proves v19 inputs can be derived without creating a
  suite, calling a provider, or mutating Gateway/Minibook.

- [ ] **Step 1: Write failing CLI/preflight tests**

Assert interrupted output retains exit `124`, exposes only redacted resume
bindings, and exits non-zero. Assert `-Action Plan` performs no provider or live
service call and reports the immutable next suite version as `v19`.

- [ ] **Step 2: Run the script tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/scripts/test_run_business_benchmark_demo.py
```

Expected: failures for missing interruption and v19 dry-run output.

- [ ] **Step 3: Add redacted recovery output and v19 dry-run preparation**

Expose only identifiers/digests needed for a Captain operator to issue the
runtime authorization. Do not expose prompt, stderr, credentials, JSONL bodies,
or private benchmark answers. Keep the secure wrapper process-only for the key.

- [ ] **Step 4: Update the gap ledger without claiming a live run**

Mark streaming/recovery implementation complete only after tests. Leave the
provider-backed v19 and 30-case benchmark checkbox open, recording that v19 is
prepared but deliberately not executed in this non-paid goal.

- [ ] **Step 5: Run the complete verification gate**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not live"
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
.\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config gateway
.\.venv\Scripts\python.exe scripts/verify_submission.py
git diff --check
```

Expected: every command exits `0`; report skipped/deselected tests separately.

- [ ] **Step 6: Commit Task 4**

```powershell
git add scripts/run-business-benchmark-demo.ps1 scripts/run-business-benchmark-demo-secure.ps1 tests/scripts/test_run_business_benchmark_demo.py docs/superpowers/plans/2026-07-28-business-benchmark-live-bootstrap-gaps.md
git commit -m "chore: prepare Factory benchmark v19 recovery"
```

Expected: clean worktree, no secrets or live evidence, and no paid calls.

## Self-review

- Spec coverage: streaming, `124` evidence, three phases, same-attempt Captain
  retry, no repeated Hermes/suite work, v19 ordering, and paid-run boundary are
  mapped to Tasks 1-4.
- Placeholder scan: no deferred implementation placeholder is used; the open
  live benchmark is an explicit operational boundary, not missing code.
- Type consistency: checkpoint, terminal status, retry authorization, replay
  state, and runner factory names are identical across tasks.

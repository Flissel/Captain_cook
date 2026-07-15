# Unroutable Work and Architecture Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make permanently unroutable subproblems durable and visible in the ledger, extend architecture tests with cycle detection, and synchronize the architecture backlog with the current branch state.

**Architecture:** `SpawnCoordinatorAgent` publishes a frozen `SubproblemUnroutable` lifecycle event when capability resolution fails. `LedgerRecorderAgent`, still the sole writer, consumes that event and moves the accepted subproblem to `FAILED` while recording capability tags and the resolution error. The dependency checker builds an internal import graph from the same AST data already used for boundary rules and reports deterministic cycles.

**Tech Stack:** Python 3.11, Pydantic 2, asyncio, pytest, stdlib `ast`.

## Global Constraints

- Preserve the deterministic in-memory runtime and existing EventBus port.
- Keep `agenten/events/schemas.py` free of AutoGen and infrastructure imports.
- All ledger mutation continues through `LedgerRecorderAgent`.
- Do not modify Minibook, Hermes, live Docker services, credentials, or foreign worktree files.
- Use RED → GREEN for every behavior change and run the full root pytest suite before completion.

---

### Task 1: Durable unroutable lifecycle event

**Files:**
- Modify: `tests/spawning/test_coordinator.py`
- Modify: `tests/ledger_bridge/test_recorder.py`
- Modify: `agenten/events/schemas.py`
- Modify: `agenten/spawning/coordinator.py`
- Modify: `agenten/ledger_bridge/recorder.py`

**Interfaces:**
- Produces: `SubproblemUnroutable(meta, subproblem_id, capability_tags, error)`.
- Consumes: existing `CapabilityRegistry.resolve`, `EventBus.publish`, and Recorder sole-writer queue.

- [x] **Step 1: Write failing coordinator test** asserting an unresolved capability publishes one `SubproblemUnroutable` and no assignment.
- [x] **Step 2: Run `python -m pytest -q tests/spawning/test_coordinator.py -k unroutable`** and confirm failure because the event does not exist.
- [x] **Step 3: Add the frozen Pydantic event and publish it from `NoCapableAgentType` handling** with the original tags and error text.
- [x] **Step 4: Run the focused coordinator test** and confirm it passes.
- [x] **Step 5: Write failing recorder test** asserting the event moves an accepted block to `FAILED` and persists `unroutable_capability_tags` plus `failure_reason`.
- [x] **Step 6: Run the recorder test** and confirm failure because no subscription/handler exists.
- [x] **Step 7: Add the event to `RECORDED_EVENT_HANDLERS`, enqueue handler, apply method, and optional RoutedAgent adapter** using the existing transition machinery.
- [x] **Step 8: Run coordinator and recorder suites** and confirm they pass.

### Task 2: Import-cycle fitness rule

**Files:**
- Modify: `tests/test_architecture_fitness.py`
- Modify: `tests/architecture_fitness.py`

**Interfaces:**
- Produces: `find_import_cycles(root: Path, package_prefixes: tuple[str, ...]) -> list[tuple[str, ...]]`.
- Consumes: AST imports returned by `imports_in_file`.

- [x] **Step 1: Write a failing fixture test** with `sample.a -> sample.b -> sample.c -> sample.a` and assert one canonical cycle.
- [x] **Step 2: Run `python -m pytest -q tests/test_architecture_fitness.py -k cycle`** and confirm the helper is missing.
- [x] **Step 3: Build the internal module graph and implement deterministic DFS cycle detection**, canonicalizing each cycle at its lexicographically smallest module.
- [x] **Step 4: Add a repository-level assertion** that the root runtime packages contain no internal import cycle.
- [x] **Step 5: Run the architecture suite** and confirm it passes or report existing cycles without suppressing them.

### Task 3: Synchronize plans and verification evidence

**Files:**
- Modify: `docs/superpowers/plans/2026-07-15-architecture-gap-todos.md`
- Modify: `docs/WORKSTREAMS.md` only where current Git evidence makes existing status text stale.

**Interfaces:**
- Consumes: current local branch tips and completed tests.
- Produces: checked TODOs only for behavior proven by the implementation.

- [x] **Step 1: Refresh branch-tip evidence** with `git branch --verbose --no-abbrev` and remove stale claims that `feat/ledger-gateway` has no implementation delta.
- [x] **Step 2: Mark the durable unroutable event and cycle fitness test complete** after their focused tests pass.
- [x] **Step 3: Run `git diff --check`, focused tests, `python -m compileall -q agenten blockchain chats config`, and `python -m pytest -q`**.
- [x] **Step 4: Inspect `git status --short`** and ensure only scoped files plus pre-existing foreign files are present.

## Self-review

- Scope is limited to one runtime failure path, one architecture fitness capability, and evidence synchronization.
- AutoGen subscription redesign and large Recorder/Pipeline splits remain separate changes.
- No placeholder interfaces or unspecified error behavior remain in this plan.

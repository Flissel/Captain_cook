# Gateway-native delivery runtime implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement supervised Codex execution, recovery, review, and read-only
Hermes/Minibook projections through the sole-writer gateway.

**Architecture:** Gateway blocks are authoritative. Workers append claim-fenced
events; Captain appends recovery/review decisions. Projectors replay the ledger
index using durable compare-and-set cursors and idempotent external effects.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, httpx, MariaDB, pytest.

## Global constraints

- Only `gateway/` writes production MariaDB; no runtime SQLite or direct DB clients.
- New lifecycle payloads are strict, claim-fenced, and contain no secrets,
  workspace paths, or raw process output.
- Minibook and Hermes are read models only; P20 owns all live-service proof.

---

### Task 1: D02 supervised Codex adapter

**Files:** Create `agenten/execution/codex_supervisor.py` and
`tests/execution/test_codex_supervisor.py`; modify `gateway/contracts.py`,
`gateway/store.py`, `gateway/app.py`, `agenten/delivery/gateway_client.py`, and
gateway contract/auth tests.

**Interfaces:**

```python
class CodexProcessEvent(EvidenceEvent):
    process_id: str
    state: Literal["started", "heartbeat", "exited", "cancelled"]
    command_digest: str

class CodexSupervisor:
    async def run(self, request: CodexRunRequest) -> PackageExecutionResult: ...
```

- [x] Write a failing test that proves a successful run emits `codex_session`
  plus sanitized `codex_process` events, and a non-zero exit returns a typed
  failed result without revealing environment values.
- [x] Run `python -m pytest -q --no-cov tests/execution/test_codex_supervisor.py`; expect failure because the adapter is absent.
- [x] Implement an injected argument-vector runner, workspace-root guard,
  allowlisted environment, strict event contract, and claim-fenced client call.
- [x] Run focused tests plus `tests/gateway/test_gateway_contracts.py` and
  `tests/gateway/test_gateway_auth.py`; then add them to the disposable DB gate.
- [x] Commit: `feat: add supervised codex gateway evidence` (`0d8b17a`).

### Task 2: D03 recovery and reasoning slices

**Files:** Create `agenten/delivery/recovery.py` and
`tests/delivery/test_gateway_recovery.py`; modify gateway contracts/store/app,
the delivery client, and gateway contract tests.

**Interfaces:**

```python
class ReasoningSliceEvent(EvidenceEvent):
    slice_id: str
    summary_ref: str
    sha256: str

class GatewayRecoveryService:
    async def recover_expired(self, now: datetime) -> tuple[RecoveryDecision, ...]: ...
```

- [ ] Write failing tests for expiry, idempotent recovery replay, and rejection
  of chain-of-thought/workspace-path payloads.
- [ ] Run the module test; expect failure before the service exists.
- [ ] Implement Captain-owned `recovery_decision` events (`requeue` or
  `aborted_infra`) and opaque hash-bound reasoning references.
- [ ] Run focused tests, architecture tests, and the selected MariaDB gate with zero skips.
- [ ] Commit: `feat: add gateway recovery and reasoning slices`.

### Task 3: D04 evidence and independent review

**Files:** Create `agenten/review/gateway_controller.py` and
`tests/review/test_gateway_controller.py`; modify gateway contracts/store and
the delivery client plus gateway contract tests.

**Interfaces:**

```python
class ReviewDecisionEvent(EvidenceEvent):
    review_id: str
    decision: Literal["passed", "failed"]
    evidence_refs: tuple[str, ...]
```

- [ ] Write failing tests proving success needs current-iteration validation
  and a passing review, while stale/forged/failed reviews are rejected.
- [ ] Run focused tests; expect failure because review events are unknown.
- [ ] Implement Captain-only review records and the terminal projection rule;
  append `failed_after_max_iterations` after five immutable failed reviews.
- [ ] Run focused gateway tests and full disposable-MariaDB gate.
- [ ] Commit: `feat: require reviewed gateway evidence for success`.

### Task 4: D05 replay feed and projections

**Files:** Create `gateway/feed.py`, `agenten/delivery/cursor_client.py`,
`agenten/delivery/projections.py`, `tests/gateway/test_gateway_feed.py`, and
`tests/delivery/test_gateway_projections.py`; modify gateway app/store and the
Minibook client only as a read-side receiver.

**Interfaces:**

```python
GET /events?after_index=0&limit=100
PUT /consumers/{consumer_name}/cursor

async def project_once(consumer: str, projector: EventProjector) -> int: ...
```

- [ ] Write failing tests for strict index order, cursor compare-and-set,
  non-decreasing cursor values, and crash-after-effect replay.
- [ ] Run feed/projection tests; expect failure because neither API exists.
- [ ] Implement authenticated pages, MariaDB cursor rows, idempotency key
  `gateway:<consumer>:<index>`, and projectors that emit only validated,
  reviewed successful batches.
- [ ] Run gateway auth/feed/projection tests and the disposable-MariaDB gate.
- [ ] Commit: `feat: add replayable gateway delivery projections`.

### Task 5: D05 integration authority proof

**Files:** Create `tests/integration/test_gateway_delivery_runtime.py`; modify
`tests/test_architecture_fitness.py`, `tests/test_import_boundaries.py`,
`scripts/test_gateway.ps1`, `docs/ARCHITECTURE.md`, `docs/WORKSTREAMS.md`, and
`docs/DEMO.md`.

- [ ] Write a failing isolated-MariaDB trace: claim, execute, validate, review,
  project, replay; assert exactly one receiver effect per consumer/index and no
  production SQLite/direct-MariaDB import.
- [ ] Run the integration test; expect failure until D02-D05 wiring exists.
- [ ] Add composition and architecture guards. Label the result as gateway
  contract evidence, not live Codex/Hermes/Minibook evidence.
- [ ] Run `pwsh -NoProfile -File scripts/test_gateway.ps1` and `python -m pytest -q`; selected DB tests must have zero skips.
- [ ] Commit: `test: prove gateway-native delivery runtime`.

## Self-review

Tasks map one-to-one to D02, D03, D04, and D05; each starts red, uses the
gateway boundary, and finishes with an independently reviewable gate. P20
remains the only owner of real external-service evidence.

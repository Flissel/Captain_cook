# Captain Authority Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Captain the single authority from released batch through submitted result, validation run, and capability promotion.

**Architecture:** Add immutable result and validation contracts, enforce them in a domain service, and let the gateway persist transitions transactionally. Registry projection consumes only validated promotion events.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, MariaDB, pytest.

## Global Constraints

- MariaDB gateway is the sole production authority; offline adapters remain non-production.
- Holdout bodies never leave Captain validation custody.
- `batch_done:succeeded` alone never promotes a capability.
- Every command is idempotent and version-fenced.
- No changes to Minibook, its Forge pipeline, or Hermes internals.

---

### Task 1: Freeze result and validation contracts

**Files:**
- Create: `agenten/validation/results.py`
- Create: `tests/validation/test_result_contracts.py`
- Create: `tests/fixtures/contracts/work_result_submitted.v1.json`
- Create: `tests/fixtures/contracts/validation_run_recorded.v1.json`

**Interfaces:** Produces `WorkResultSubmitted`, `EvidenceRef`, `AssertionResult`, and `ValidationRunRecorded` frozen Pydantic models.

- [ ] Write failing tests requiring unique assertion IDs, SHA-256 artifact digests, exact batch version, and rejection of unknown fields.
- [ ] Run `python -m pytest tests/validation/test_result_contracts.py -v`; expect import failure.
- [ ] Implement the four frozen models with `extra="forbid"`; `ValidationRunRecorded.passed` is true only when every required assertion ID has one passing result.
- [ ] Serialize both checked-in fixtures and assert byte-stable round trips.
- [ ] Run the focused test; expect PASS.
- [ ] Commit with `feat: define captain result validation contracts`.

### Task 2: Enforce the authority transition service

**Files:**
- Create: `agenten/validation/authority.py`
- Create: `tests/validation/test_authority_service.py`
- Modify: `gateway/app.py`

**Interfaces:** Produces `submit_result(command)`, `record_validation(command)`, and `promote_capability(batch_id, expected_version)`; consumes released `WorkBatch` and private `HoldoutSuite`.

- [ ] Write failing tests proving wrong version, digest mismatch, missing assertion, duplicate command, and promotion-before-validation all fail closed.
- [ ] Run the tests and record the expected missing-service failure.
- [ ] Implement pure transition decisions first; they return typed events and never write storage directly.
- [ ] Add gateway routes that lock the batch row, apply the decision, append the event, and commit before returning.
- [ ] Prove concurrent duplicate submissions produce one event and one stable response.
- [ ] Run `python -m pytest tests/validation/test_authority_service.py tests/gateway -v`; expect PASS.
- [ ] Commit with `feat: enforce captain validation authority chain`.

### Task 3: Gate capability projection

**Files:**
- Modify: `gateway/registry_feed.py`
- Modify: `gateway/app.py`
- Modify: `tests/gateway/test_registry_feed.py`
- Modify: `tests/gateway/test_gateway.py`

**Interfaces:** Consumes `CapabilityPromoted.v1`; no longer consumes bare `batch_done` for registration.

- [ ] Add a failing test showing `batch_done:succeeded` without a validation-run reference emits no registry payload and creates no capability row.
- [ ] Add a passing fixture for `capability-promoted.v1` referencing batch version, validation run ID, artifact digest, and capability tags.
- [ ] Change registry feed and gateway upsert to accept only that event.
- [ ] Run gateway tests; expect PASS with no MariaDB tests silently skipped in the integration gate.
- [ ] Commit with `fix: require validation before capability promotion`.

### Task 4: Prove production and offline boundaries

**Files:**
- Create: `tests/contracts/test_captain_authority_boundary.py`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `README.md`

**Interfaces:** Documents MariaDB authority and offline adapter limitations.

- [ ] Add import-boundary tests preventing Captain validation from importing Minibook or Hermes internals.
- [ ] Add a gateway integration case covering release→result→validation→promotion and replay.
- [ ] Run the isolated MariaDB gateway gate with zero skips, then the full pytest and submission verifier.
- [ ] Document only the behavior evidenced by those commands.
- [ ] Commit with `docs: define captain authority boundary`.

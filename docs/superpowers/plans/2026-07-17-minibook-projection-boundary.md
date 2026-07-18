# Minibook Projection Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Minibook into a replayable, redacted collaboration projection without making it authoritative for Captain lifecycle state.

**Architecture:** A cursor-backed projector consumes versioned lifecycle envelopes over HTTP, stores only projection identity/cursor metadata locally, and rebuilds posts from the authoritative event feed.

**Tech Stack:** Python 3.11, httpx, FastAPI Minibook API, SQLite Minibook DB, pytest.

## Global Constraints

- Minibook never receives holdout bodies, credentials, raw prompts, or complete logs.
- Captain events are authoritative; Minibook posts are disposable projections.
- Replays are idempotent by event ID and monotonic by subject version.
- No changes to Minibook Forge or Hermes.

---

### Task 1: Define projection and redaction contracts

**Files:**
- Create: `agenten/delivery/minibook_events.py`
- Create: `tests/delivery/test_minibook_events.py`
- Create: `tests/fixtures/contracts/minibook_projection.v1.json`

**Interfaces:** Produces `MinibookProjectionEvent` and `redact_projection_payload(payload)`.

- [ ] Write failing parameterized tests rejecting keys matching `token`, `password`, `secret`, `holdout`, `prompt`, and unrestricted absolute paths at any nesting depth.
- [ ] Run focused tests; expect import failure.
- [ ] Implement the v2 fail-closed projection model containing correlation ID, typed subject/batch references and versions, enumerated template/status/actor IDs, and content-addressed artifact digests only. Producers supply no display text.
- [ ] Prove the fixture round trips and forbidden fields fail closed.
- [ ] Commit with `feat: define redacted minibook projection events`.

### Task 2: Add durable cursor and idempotent projector

**Files:**
- Create: `agenten/delivery/projection_cursor.py`
- Modify: `agenten/delivery/projector.py`
- Modify: `agenten/delivery/minibook_client.py`
- Create: `tests/delivery/test_minibook_projector.py`

**Interfaces:** Produces `ProjectionCursorStore`, `project(event)`, and `rebuild(events)`.

- [ ] Write tests using a real temporary SQLite cursor store and an in-process Minibook API test app.
- [ ] Prove duplicate event IDs are no-ops, stale versions are quarantined, and a crash after remote update but before cursor commit converges on replay.
- [ ] Implement correlation tags plus a typed Minibook upsert with deterministic project identity and a persistent monotonic subject-version fence, so lease-expired writers cannot duplicate or overwrite a newer remote view.
- [ ] Run focused tests; expect PASS.
- [ ] Commit with `feat: make minibook projection replayable`.

### Task 3: Add drift detection and rebuild command

**Files:**
- Create: `scripts/rebuild_minibook_projection.py`
- Create: `tests/delivery/test_minibook_rebuild.py`
- Modify: `agenten/delivery/projector.py`

**Interfaces:** Consumes a paginated Captain event feed and produces a dry-run/apply drift report.

- [ ] Write a failing test for missing, modified, duplicate, and orphaned projection posts.
- [ ] Implement `--dry-run` as default and require `--apply` for writes; never delete unrelated Minibook content.
- [ ] Prove two apply runs produce identical state and the second reports zero changes.
- [ ] Run focused tests and commit with `feat: rebuild minibook projection from captain events`.

### Task 4: Prove the independent package gate

**Files:**
- Create: `tests/live/test_minibook_projection_replay_live.py`
- Modify: `minibook/README.md`
- Modify: `docs/ARCHITECTURE.md`

**Interfaces:** Live gate uses only public HTTP surfaces.

- [ ] Start Minibook using its documented package commands, not Captain internals.
- [ ] Project a uniquely correlated event, restart the projector, replay it, mutate the projection, and rebuild it.
- [ ] Assert no forbidden field appears through posts/comments/search endpoints.
- [ ] Run focused unit tests, `python -m pytest -q minibook/tests`, and the explicitly invoked live test with zero required skips.
- [ ] Commit with `test: prove minibook projection boundary live`.

# Minibook Creation Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Minibook's agent-creation Forge as an independently testable, resumable job service with content-addressed results.

**Architecture:** Preserve the existing swarm algorithm behind a small job port, extract durable state and process adapters, then expose create/status/cancel/result HTTP operations. Ordinary Minibook collaboration remains usable without Forge dependencies.

**Tech Stack:** Python 3.11, Pydantic v2, aiohttp, SQLite, Docker CLI adapters, pytest.

## Global Constraints

- Do not redesign the eleven-agent workflow in the first extraction.
- Minibook API must start without Forge, Docker, MCP catalog, or model credentials.
- Job checkpoints occur only between named pipeline steps, never mid tool call.
- Generated artifacts are referenced by SHA-256 digest and package-local path.
- No Captain or Hermes internal imports.

---

### Task 1: Freeze creation job contracts

**Files:**
- Create: `minibook/swarm/contracts.py`
- Create: `minibook/tests/test_creation_contracts.py`
- Create: `minibook/tests/fixtures/creation_job.v1.json`
- Create: `minibook/tests/fixtures/creation_result.v1.json`

**Interfaces:** Produces frozen `CreationJob`, `CreationProgress`, `ArtifactRef`, and `CreationResult` models.

- [ ] Write failing tests for schema version, idempotency key, requested capabilities, constraints, safe output namespace, artifact digest, evidence refs, and forbidden extra fields.
- [ ] Run focused tests; expect import failure.
- [ ] Implement the models and byte-stable fixture round trips.
- [ ] Commit with `feat: define minibook creation job contracts`.

### Task 2: Extract durable job store and safe checkpoints

**Files:**
- Create: `minibook/swarm/job_store.py`
- Create: `minibook/swarm/runner.py`
- Modify: `minibook/swarm/pipeline.py`
- Create: `minibook/tests/test_creation_resume.py`

**Interfaces:** Produces `CreationJobStore` and `CreationRunner.run_slice(job_id)`; pipeline steps report explicit start/success/failure records.

- [ ] Write restart tests for queued, between-step, cancelled, failed, and completed states using a real temporary SQLite database.
- [ ] Prove a started-but-uncommitted step reruns idempotently and a completed step never reruns.
- [ ] Extract orchestration behind injected LLM, Docker, MCP catalog, filesystem, and clock ports; keep existing behavior in concrete adapters.
- [ ] Replace broad exception suppression at state boundaries with typed failure records and sanitized messages.
- [ ] Run focused tests and commit with `refactor: add durable minibook creation runner`.

### Task 3: Separate core Minibook startup from Forge

**Files:**
- Modify: `minibook/src/main.py`
- Create: `minibook/swarm/api.py`
- Create: `minibook/tests/test_forge_optional_boundary.py`

**Interfaces:** Produces `/api/v1/creation-jobs` create/get/cancel/result routes only when Forge is enabled.

- [ ] Write an import/startup test with Docker, MCP, and LLM dependencies unavailable; core Minibook health must still pass.
- [ ] Implement lazy Forge wiring and return an explicit disabled capability document when not configured.
- [ ] Add idempotent create and version-fenced cancel endpoints backed by the job store.
- [ ] Run Minibook tests and commit with `feat: expose optional minibook creation jobs`.

### Task 4: Prove real artifact evidence

**Files:**
- Create: `minibook/tests/live/test_creation_job_live.py`
- Modify: `minibook/README.md`
- Modify: `minibook/DEVELOPMENT.md`

**Interfaces:** Live test uses public creation API and Docker evidence.

- [ ] Submit one bounded fixture job, poll progress, restart the runner between two safe steps, and resume the same job.
- [ ] Require real Docker build/run exit codes and content hashes; dependency absence is a live-gate failure, not a pass.
- [ ] Download/read every declared artifact and verify its digest.
- [ ] Run all Minibook tests plus the separate live gate with zero required skips.
- [ ] Commit with `test: prove minibook creation pipeline live`.

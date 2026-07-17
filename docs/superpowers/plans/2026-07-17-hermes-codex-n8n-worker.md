# Hermes Codex and n8n MCP Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hermes an independent worker that accepts Captain work envelopes, supervises real Codex sessions, and returns correlated n8n MCP evidence.

**Architecture:** Implement the worker inside the Hermes repository using its existing Codex runtime and MCP configuration surfaces. A transport-neutral inbox/outbox adapter keeps Captain decoupled; the parent repository records only the reviewed submodule commit.

**Tech Stack:** Hermes Python runtime, Codex CLI/app server, MCP, n8n, pytest.

## Global Constraints

- Implement in a dedicated Hermes submodule branch; parent changes only pin the reviewed commit and add compatibility fixtures/docs.
- Do not duplicate Hermes Codex runtime inside Captain.
- VibeMind owns n8n; the worker may health-check and call it but never manage its containers or volumes.
- Commands use argument arrays, authorized worktree roots, and sanitized JSONL evidence.
- Missing Codex/n8n live evidence fails the live gate; it is never replaced with mock evidence.

---

### Task 1: Define the worker envelope in Hermes

**Files (Hermes repository):**
- Create: `hermes_cli/worker_contracts.py`
- Create: `tests/hermes_cli/test_worker_contracts.py`
- Create: `tests/fixtures/captain_work_package_released.v1.json`
- Create: `tests/fixtures/hermes_work_result_submitted.v1.json`

**Interfaces:** Produces strict `CaptainWorkPackage`, `HermesWorkResult`, `CodexEvidenceRef`, and `McpEvidenceRef` models matching Captain fixtures.

- [ ] Write failing fixture tests for schema/event/correlation IDs, batch version, authorized workspace, acceptance assertion IDs, artifact digest, Codex session ID, and n8n execution/call ID.
- [ ] Reject holdout bodies, credentials, and unknown fields.
- [ ] Implement strict models and prove byte-compatible fixture round trips.
- [ ] Commit in Hermes with `feat: define captain worker envelopes`.

### Task 2: Supervise Codex work through existing Hermes runtime

**Files (Hermes repository):**
- Create: `hermes_cli/captain_worker.py`
- Create: `tests/hermes_cli/test_captain_worker.py`
- Modify only the existing Codex entrypoint needed to expose start/resume/status/cancel as an injected port.

**Interfaces:** Produces `claim`, `start`, `heartbeat`, `resume`, `cancel`, and `collect_result`; consumes existing Hermes Codex runtime APIs.

- [ ] Write tests from a sanitized capture of a benign real Codex JSONL session; prove session ID parsing, exit state, before/after commit, and changed-path confinement.
- [ ] Prove paths outside the authorized worktree and symlink escapes fail before process start.
- [ ] Implement worker orchestration without shell-concatenated prompts or environment logging.
- [ ] Persist resumable session metadata and emit monotonic heartbeat/result envelopes.
- [ ] Run focused Hermes tests and commit with `feat: supervise captain codex work`.

### Task 3: Add n8n MCP preflight and evidence adapter

**Files (Hermes repository):**
- Create: `hermes_cli/n8n_worker_mcp.py`
- Create: `tests/hermes_cli/test_n8n_worker_mcp.py`
- Modify: `hermes_cli/mcp_config.py`

**Interfaces:** Produces `discover_capabilities()` and `invoke_tool(name, arguments, correlation_id)` using Hermes generic MCP configuration.

- [ ] Write tests proving configured-server selection, exact tool allow-listing, correlation propagation, timeout, redaction, and fail-closed behavior.
- [ ] Implement health/capability discovery without Docker or volume operations.
- [ ] Record server identity, tool name, call/execution ID, timestamps, input digest, and output digest; never record credentials or unrestricted payloads.
- [ ] Run focused tests and commit with `feat: collect n8n mcp worker evidence`.

### Task 4: Add transport-neutral inbox/outbox worker loop

**Files (Hermes repository):**
- Create: `hermes_cli/captain_worker_loop.py`
- Create: `tests/hermes_cli/test_captain_worker_loop.py`

**Interfaces:** Consumes an injected inbox and publishes through an injected outbox; first concrete adapter may be HTTP, but domain logic has no Captain imports.

- [ ] Write tests for duplicate delivery, stale subject version, lease expiry, restart, cancellation, and publish-after-persist ordering.
- [ ] Implement exactly-once effects through idempotency records while allowing at-least-once transport delivery.
- [ ] Prove restart resumes the same Codex session and never starts a duplicate process.
- [ ] Run focused tests and commit with `feat: run hermes captain worker loop`.

### Task 5: Prove live Codex and n8n MCP execution

**Files (Hermes repository):**
- Create: `tests/live/test_captain_worker_codex_n8n_live.py`
- Modify: `website/docs/user-guide/features/kanban-worker-lanes.md`

**Interfaces:** Uses installed Codex and configured external VibeMind n8n MCP.

- [ ] Create a disposable authorized Git worktree and a bounded work envelope.
- [ ] Run real Codex, require a real session ID, and verify changes remain inside the worktree.
- [ ] Discover/invoke one read-only or isolated n8n MCP operation and require a real call/execution ID.
- [ ] Submit a correlated result envelope, restart the worker, replay the input, and prove no duplicate Codex/n8n effect.
- [ ] Run focused Hermes tests and the explicit live gate with zero required skips.
- [ ] Commit in Hermes, then update only the parent submodule pin on an integration branch after review.

### Task 6: Cross-repository compatibility gate

**Files (parent Captain repository):**
- Create: `tests/contracts/test_work_package_compatibility.py`
- Add reviewed fixture copies under: `tests/fixtures/contracts/`
- Modify: `.gitmodules` only if the declared source is incorrect; otherwise leave it unchanged.

**Interfaces:** Compares canonical JSON schemas/fixtures without importing Hermes.

- [ ] Validate Captain producer fixtures and Hermes consumer fixtures have identical schema IDs and required fields.
- [ ] Prove forbidden secret/holdout fields fail both sides.
- [ ] Run compatibility test, full parent suite, submission verifier, and inspect `git diff --submodule=log`.
- [ ] Commit parent pin/fixtures with `chore: pin hermes captain worker contract`.

# Production Authority Assembly and Reproducible Live Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Captain Cook to a reproducible live demo: secure and review-cleanly integrate the already verified Task-6 commits A–C, implement a generic production authority assembly contract for three arbitrary repository-owned `TO_BE_BUILT.md` inputs, provide persistent fail-closed Gateway endpoints with sole-writer evidence for Resume Authorize → Dispatch → Readback, build one concrete digest-pinned Captain/Gateway/Runtime/Minibook/n8n authority adapter bundle, harden the adapter loader with the existing TOCTOU-safe single-open procedure, execute the complete offline E2E, architecture, restart/recovery, cost and credential gates, then preflight services and run one controlled real live demo with three conversation patterns, Minibook projection and immutable evidence.

**Architecture:** Captain remains the only lifecycle, retry and seal authority. The assembly contract compiles per-repository `TO_BE_BUILT.md` intent into an executor-neutral authority assembly that names every adapter by digest. The Gateway persists resume authorization, dispatch and readback evidence as the sole MariaDB writer. Adapters are loaded only through the single-open, descriptor-verified read already proven in `agenten/agent_factory/codex_build_execution.py`. Minibook stays a read-only projection; n8n access stays behind Captain-approved leases.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, MariaDB, pytest, PowerShell 7, Codex CLI/app-server, n8n MCP, Minibook.

## Global Constraints

- No invented IDs or receipts: every session, claim, deployment, execution and evidence ID must originate from the system that owns it.
- No claims/renewal reuse: a consumed or expired claim is never re-presented; renewals are single-use and monotonic.
- No mock evidence counted as live success: mocked or skipped checks stay classified as offline evidence.
- No secrets in Git: credentials live only in gitignored `.env` files or user-level tool config.
- No uncontrolled provider costs: every provider-backed step runs under an explicit budget with a hard call ceiling and the existing deferred-batch rule (an interrupted provider session is never blindly requeued).
- Execution environment split is explicit: tasks marked **[container-ok]** run anywhere with Python 3.11 and an isolated MariaDB; tasks marked **[windows-host]** require the owner's Windows 11 machine with Docker, n8n, Mailpit, Minibook and provider credentials, and can only be executed there.

## Session evidence recorded at planning time (2026-08-11/12, Linux container)

- Task 6 (Gateway benchmark persistence and promotion gate) is merged to `main` via `be7a4a8`; no unmerged `codex/*` branch remains on the remote.
- Offline gates already green in the container: `python main.py demo` → 4 subproblems done; `tests/gateway/` with isolated MariaDB → 507 passed, 2 skipped; architecture/import/workstream gates → 18 passed; `scripts/verify_submission.py` → pass.
- Full `-m "not live"` suite on Linux: 2870 passed, 73 failed — every failure is Windows-platform-specific (batch-file fixtures, `%SystemRoot%` tooling, Windows listener identity); CI runs these on self-hosted Windows runners.

---

### Task 0: Secure and integrate Task-6 commits A–C **[windows-host]**

**Files:** none in this repository until the commits are pushed.

The commits named A–C exist only in the owner's local clone. From any other
environment they are unverifiable; nothing may be "recreated" in their place.

- [ ] **Step 1:** On the Windows host, run the worktree/dirty-state/ancestry audit from `docs/WORKSTREAMS.md` and locate commits A–C.
- [ ] **Step 2:** Push them to a dedicated branch (`codex/task6-commits-a-c`) without rebase; record the exact SHAs here.
- [ ] **Step 3:** Open a PR, run the focused Task-6 suites (`tests/gateway/test_agent_factory.py`, `tests/gateway/test_factory_repository.py`, `tests/agent_factory/test_state_machine.py`, `tests/agent_factory/test_release_gate.py`) and integrate only after review.
- [ ] **Step 4:** If the commits duplicate what `be7a4a8` already merged, close the branch with a note instead of force-integrating.

### Task 1: Generic production authority assembly contract **[container-ok]**

**Files:** Create `agenten/agent_factory/authority_assembly_contracts.py`; create `tests/agent_factory/test_authority_assembly_contracts.py`; create three fixtures `tests/fixtures/to_be_built/<repo-a|repo-b|repo-c>/TO_BE_BUILT.md`.

**Interfaces:** `AuthorityAssemblyV1(assembly_id, source_repository, input_sha256, adapters: tuple[AuthorityAdapterRefV1, ...], created_at)` and `AuthorityAdapterRefV1(role: Literal["captain","gateway","runtime","minibook","n8n"], artifact_uri, sha256, version)`. `assemble_production_authority(document: FactoryInputDocumentV2, adapters) -> AuthorityAssemblyV1` is deterministic: same input bytes and adapter set produce byte-identical canonical JSON.

- [ ] **Step 1:** Write failing tests: three distinct repository-owned `TO_BE_BUILT.md` fixtures compile into three assemblies; a credential-bearing input is rejected; an adapter ref without a 64-hex digest is rejected; unknown roles are rejected; canonical serialization is byte-stable.
- [ ] **Step 2:** Run `python -m pytest -q --no-cov tests/agent_factory/test_authority_assembly_contracts.py` and confirm RED.
- [ ] **Step 3:** Implement the frozen contracts reusing `FactoryInputDocumentV2` parsing from `agenten/agent_factory/input_contracts.py`/`input_document.py`; do not duplicate the input parser.
- [ ] **Step 4:** Confirm GREEN, then run the import-boundary gate (`tests/test_import_boundaries.py`).
- [ ] **Step 5:** Commit `feat(agent-factory): add production authority assembly contract`.

### Task 2: Fail-closed Gateway endpoints and sole-writer evidence for Resume Authorize → Dispatch → Readback **[container-ok]**

**Files:** Modify `gateway/contracts.py`, `gateway/store.py`, `gateway/app.py`; create `tests/gateway/test_authority_resume_flow.py`.

**Interfaces:** `POST /v1/authority/assemblies/{assembly_id}/resume-authorizations` (Captain-only, single-use, bounded TTL), `POST .../dispatches` (requires an unconsumed authorization; consumes it transactionally), `GET .../readback` (returns only persisted, redacted evidence). The Gateway remains the sole MariaDB writer; every transition is one transaction with a monotonic revision.

- [ ] **Step 1:** Write failing tests: dispatch without authorization → 403; double-consume of one authorization → 409; readback of unknown assembly → 404; evidence rows are append-only (an UPDATE attempt has no path); restart of the app process keeps consumed state (MariaDB-backed, `TEST_MARIADB_DSN`).
- [ ] **Step 2:** Confirm RED with the isolated MariaDB DSN.
- [ ] **Step 3:** Implement schema through `GatewayStore._ensure_schema` (same pattern as `factory_business_benchmark_summaries`), with `FOR UPDATE` locking and single-transaction consume, following the Task-6 report conventions.
- [ ] **Step 4:** Confirm GREEN, rerun `tests/gateway/` completely.
- [ ] **Step 5:** Commit `feat(gateway): add fail-closed authority resume flow`.

### Task 3: Concrete digest-pinned authority adapter bundle **[container-ok]**

**Files:** Create `agenten/agent_factory/authority_adapter_bundle.py`; create `tests/agent_factory/test_authority_adapter_bundle.py`; create `config/authority-adapter-bundle.v1.json`.

**Interfaces:** `AuthorityAdapterBundleV1` pins one adapter per role (captain, gateway, runtime, minibook, n8n) by artifact URI + SHA-256, mirroring the digest verification in `agenten/agent_factory/gitea_templates.py` (`sha256(body) != release.sha256 → error`). The bundle file itself is content-addressed; loading verifies the recorded digest of every entry before any adapter is used.

- [ ] **Step 1:** Write failing tests: bundle with a missing role → error; digest mismatch on load → error naming the role but never the content; identical bundle bytes → identical bundle digest.
- [ ] **Step 2:** Confirm RED, implement, confirm GREEN.
- [ ] **Step 3:** Commit `feat(agent-factory): pin authority adapter bundle by digest`.

### Task 4: Harden the adapter loader with the existing TOCTOU-safe single-open **[container-ok]**

**Files:** Modify `agenten/agent_factory/authority_adapter_bundle.py`; extend `tests/agent_factory/test_authority_adapter_bundle.py`.

**Interfaces:** Reuse the proven procedure from `agenten/agent_factory/codex_build_execution.py` (single `os.open` with `O_NOFOLLOW`, `os.fstat` identity capture, digest computed from the same descriptor, post-read `fstat` compare). Extract it into one shared helper rather than copying it; both call sites keep their behavior.

- [ ] **Step 1:** Write failing tests: a symlinked bundle path is rejected; a file replaced between open and read is rejected (identity mismatch); the happy path returns the verified bytes exactly once.
- [ ] **Step 2:** Confirm RED, extract the shared single-open helper, wire both call sites, confirm GREEN including the existing `tests/agent_factory/test_codex_build_execution.py`.
- [ ] **Step 3:** Commit `refactor(agent-factory): share toctou-safe single-open loader`.

### Task 5: Complete offline gates **[container-ok]**

- [ ] Run `python -m pytest -q -m "not live"` with `TEST_MARIADB_DSN` set; on non-Windows, report the known Windows-only failures separately and require zero non-platform failures.
- [ ] Run `python -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py`.
- [ ] Run `python -m compileall -q agenten blockchain chats config gateway`.
- [ ] Run `python main.py demo --output artifacts/demo-run.json` only if demo evidence is intentionally refreshed; otherwise write to a scratch path.
- [ ] Run `python scripts/verify_submission.py`.
- [ ] Cost gate: assert every provider-facing config used by Tasks 1–4 carries an explicit budget ceiling; credential gate: `git grep` proves no new secret material.

### Task 6: Service preflight **[windows-host]**

- [ ] Run `.\status.ps1 -Detailed` and `scripts/verify_delivery_stack.ps1`; MariaDB, Mailpit, Gateway `/healthz`, n8n and Minibook must be reachable.
- [ ] Run `python main.py recover-gateway` once; only durable `recovered_batch_ids` proceed, `deferred_batch_ids` stay deferred.
- [ ] Run `scripts/verify_hermes_readiness.ps1`; the submodule must match the parent gitlink.

### Task 7: Controlled real live demo **[windows-host]**

- [ ] Define the three conversation patterns as fixtures before the run; record their digests.
- [ ] Opt in explicitly (dedicated environment variables, never committed), set the provider budget ceiling, and start the bounded live run.
- [ ] Require for success: real provider session IDs, Gateway-persisted dispatch and readback evidence for one correlation ID, Minibook projection rebuilt from the Gateway feed, and an immutable evidence artifact digest recorded in this file.
- [ ] Absent prerequisites leave this task BLOCKED; it is never satisfied by mocks, and a failed run is recorded as failed.

## Plan Self-Review

- Spec coverage: Tasks 0–7 map one-to-one onto the stated goal sentence, in dependency order.
- Environment honesty: every task is labeled [container-ok] or [windows-host]; the live demo cannot be produced outside the owner's infrastructure and is not claimed otherwise.
- Reuse: input parsing (`input_contracts.py`), digest verification (`gitea_templates.py`), sole-writer schema pattern (Task-6 report), and the TOCTOU single-open (`codex_build_execution.py`) are reused, not duplicated.

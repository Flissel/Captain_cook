# TO_BE_BUILT Capability Factory Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one canonical `TO_BE_BUILT.md` into a reusable, versioned AutoGen capability package built through Hermes skills and Minibook's `SwarmPipeline`, then release it only after Captain-owned real-case, recovery, restart, and three-run evidence is complete.

**Architecture:** Captain compiles immutable intent and remains lifecycle authority. Hermes uses one digest-verified released skill to design and assign work. Minibook Forge owns resumable build sequencing and calls the existing Hermes/Codex and n8n capability boundaries. The Gateway stores authoritative outcomes and emits a redacted Minibook projection. The work is split into three file-exclusive packages so three sessions can work asynchronously after the shared input contract lands.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, MariaDB Gateway, AutoGen 0.7.5, Hermes CLI, Codex CLI/app-server, n8n MCP/REST, Minibook `SwarmPipeline`, pytest, PowerShell.

## Global Constraints

- Implement the approved design in `docs/superpowers/specs/2026-07-21-to-be-built-input-outcome-design.md`; do not weaken its input, authority, evidence, or terminal-state rules.
- `TO_BE_BUILT.md` is immutable build input. The existing root `input.md` remains the factory meta-document and is never silently accepted as a per-build request.
- Captain/MariaDB is the only lifecycle and release authority. Hermes, Codex, AutoGen, n8n, SwarmPipeline, and Minibook cannot select a terminal state or publish a capability.
- Minibook is an independent product and read-only projection consumer. Parent production modules must not import `minibook.*`.
- `hermes-agent/` is a Git submodule. Implement Hermes changes on a dedicated submodule branch; the parent records only a reviewed submodule commit.
- n8n is used only for declared external integrations and only through a Captain-approved `integration_intent=n8n` lease. AutoGen owns reasoning.
- Required `TODO_TOOL.v1` gaps block release. Optional gaps are non-blocking only when a tested fallback satisfies every linked assertion.
- Preserve the five behavioral-iteration ceiling and add a total wall-clock budget. Infrastructure retries do not create duplicate Codex or n8n effects.
- Provider-backed evidence, controlled recovery, restart/resume, and three distinct clean E2E runs are explicit live gates. Mocked or skipped checks are never counted as green.
- Never commit credentials, private holdout bodies, raw model transcripts, unrestricted paths, or n8n secrets.
- Before each branch or worktree is created, run the worktree/dirty-state/ancestry audit from `docs/WORKSTREAMS.md`. Never delete a worktree or branch as part of this program.

---

## Current Baseline and Gaps to Close

| Area | Reusable baseline | Gap to close |
| --- | --- | --- |
| Factory input | `agenten/agent_factory/input_document.py` content-addresses the old `input.md` contract | Exact `TO_BE_BUILT.md` schema, nested agent/integration parsing, credential rejection, deterministic compilation, private holdout refs, and legacy migration preflight |
| Skill use | `ReleasedHermesSkill`, usage receipts, private candidates, and `TODO_TOOL.v1` validation exist | Bind the skill to a complete Forge creation job, documentation provenance, Codex assignments, and the resulting capability package |
| Build execution | `MinibookSwarmForge` starts `minibook/autogen_swarm.py`; `SwarmPipeline` already builds/tests/exports | Durable creation job/status/result, exactly-once submission, named checkpoints, restart, and content-addressed result receipt |
| Codex/n8n | Hermes submodule already has scoped Codex and n8n MCP worker surfaces | Connect those surfaces to Forge work items and require code/workflow/API artifacts plus real evidence |
| Release | Factory release gate already requires recovery followed by three clean E2E runs | Complete package manifest, four Captain-derived terminal states, wall-clock enforcement, capability catalog, and execution outcomes |
| Runtime/demo | HTTP executor expects `/v1/runtime/execute`; Gateway persists commands/grants/results | Host the runtime endpoint and project correlated runtime-result events through the Gateway feed |
| Minibook | Promotion projection exists | One ordered, paginated feed for both capability promotion and execution outcomes, with redaction and replay proof |

## Work Package Ownership

| Package | Plan | Exclusive primary paths | Depends on | Produces |
| --- | --- | --- | --- | --- |
| A — Input and compilation | `2026-07-21-to-be-built-ingestion-implementation.md` | `agenten/agent_factory/input_*`, `job_builder.py`, `contracts.py`, input fixtures/tests | Approved design only | `AgentFactoryJob.v2`, compiled DAG/assertions, private holdout refs, catalog lookup request |
| B — Hermes/Forge build | `2026-07-21-hermes-swarm-build-implementation.md` | `minibook/swarm/*`, `minibook/autogen_swarm.py`, `agenten/agent_factory/forge_*`, `minibook_forge.py`, scoped Hermes submodule files | Package A fixtures/contracts | Resumable `CreationResult.v1`, Codex/n8n/tool artifacts, skill receipt and candidate evidence |
| C — Outcome and release | `2026-07-21-capability-outcome-release-implementation.md` | `outcome_contracts.py`, `state_machine.py`, `release_gate.py`, `service.py`, `gateway/*`, runtime HTTP/projection files | Package A contracts; Package B result fixture | Authoritative terminal state, capability catalog entry, execution outcome, projection, live release evidence |

No two active sessions may edit the same primary file. Cross-package contracts travel as committed JSON fixtures first. A consumer pins the fixture digest and must not import the producer's implementation package.

## Asynchronous Delivery Graph

```text
                    A1 strict input contract
                              |
                    A2 compiled job fixture
                      /               \
          B1 creation contracts      C1 outcome contracts
                    |                  |
          B2 durable SwarmPipeline    C2 terminal-state policy
                    |                  |
          B3 Hermes/Codex/n8n build   C3 Gateway/runtime/projection
                      \               /
                       integration branch
                              |
                  controlled recovery + 3 E2E
                              |
                        ready_to_use
```

Sessions B and C may start as soon as Package A commits the versioned fixtures. They develop against copied fixtures until integration; neither waits for the other's implementation.

## Integration Protocol

- [ ] **Step 1: Establish the clean integration baseline**

Run:

```powershell
git fetch origin --prune
git status --short --branch
git worktree list --porcelain
git rev-list --left-right --count origin/main...HEAD
git diff --stat
```

Expected: user-owned `input.md` and `TO_BE_BUILT.md` changes are identified and excluded from all implementation commits. Record the exact integration SHA.

- [ ] **Step 2: Deliver Package A contract fixtures first**

Execute Tasks 1–4 in `2026-07-21-to-be-built-ingestion-implementation.md`. Publish only after its focused tests and contract fixture round trips pass.

Required handoff:

```text
schema: captain.agent-factory-job.v2
fixture: tests/fixtures/agent_factory/agent_factory_job.v2.json
input fixture: tests/fixtures/agent_factory/TO_BE_BUILT.valid.md
contract commit: recorded Package A commit SHA
```

- [ ] **Step 3: Start Packages B and C from the same contract commit**

Create separate `codex/` branches/worktrees only after the user authorizes execution. Package B consumes the job fixture; Package C consumes the job and creation-result fixtures. Each branch records its base SHA and never edits another package's primary paths.

- [ ] **Step 4: Validate each package independently**

Package A gate:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_document.py tests/agent_factory/test_input_compiler.py tests/agent_factory/test_job_builder.py tests/agent_factory/test_capability_resolution.py
```

Package B gate:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov minibook/tests/test_creation_contracts.py minibook/tests/test_creation_resume.py minibook/tests/test_creation_api.py tests/agent_factory/test_minibook_forge.py tests/contracts/test_forge_contract_compatibility.py
git -C hermes-agent status --short --branch
```

Package C gate:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_outcome_contracts.py tests/agent_factory/test_state_machine.py tests/agent_factory/test_release_gate.py tests/gateway/test_agent_factory.py tests/gateway/test_agent_runtime.py tests/gateway/test_registry_feed.py tests/agent_runtime/test_http_server.py
```

- [ ] **Step 5: Simulate integration before merging**

Use a temporary integration branch/worktree. Merge with `--no-commit`, inspect overlap, run the deterministic gates, then abort the simulation. Explicitly inspect `requirements*.txt`, `.env.example`, `README.md`, `main.py`, `docs/ARCHITECTURE.md`, `gateway/app.py`, and the submodule pointer.

- [ ] **Step 6: Integrate in dependency order**

Merge A → B → C. After each merge, rerun that package's focused gate plus:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
.\.venv\Scripts\python.exe -m compileall -q agenten gateway blockchain chats config
.\.venv\Scripts\python.exe scripts/verify_submission.py
```

- [ ] **Step 7: Run the full deterministic regression gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not live"
.\.venv\Scripts\python.exe main.py demo --output artifacts/demo-run.json
```

Do not rewrite `artifacts/demo-run.json` unless the accepted demo contract intentionally changed. Report exact passed/skipped counts and warnings.

- [ ] **Step 8: Run the provider-backed capability release gate last**

After all database-resetting tests:

```powershell
pwsh -NoProfile -File scripts/run-capability-factory-live.ps1 -InputPath .\TO_BE_BUILT.md
```

The script must preflight the canonical input, create/resume one correlated Factory job, run one controlled recovery case, run exactly three subsequent distinct successful E2E cases, verify Gateway/MariaDB state, verify real Codex/n8n evidence where declared, rebuild the Minibook projection, and emit one redacted manifest. Any required skip, unresolved required tool, missing runtime endpoint, missing projection event, or service race blocks the gate.

## Program Definition of Done

- [ ] Root `TO_BE_BUILT.md` passes the strict preflight without credential values or review markers.
- [ ] Identical input bytes replay the same job; changed bytes require a new subject version.
- [ ] A compatible released capability is reused without starting Forge.
- [ ] A catalog miss submits one idempotent Minibook creation job and receives one content-addressed result.
- [ ] Hermes proves use of the released AutoGen factory skill and records documentation/tool decisions.
- [ ] Codex creates importable AutoGen code plus only the n8n workflows/adapters required by the input.
- [ ] Every missing integration is either implemented and tested or represented by a correctly classified `TODO_TOOL.v1`.
- [ ] Captain independently derives and validates all public assertions and private holdouts.
- [ ] Recovery, restart/resume, and three consecutive clean E2E runs share a complete authoritative evidence chain.
- [ ] Gateway stores one immutable capability package and a later typed execution outcome.
- [ ] Minibook can rebuild the redacted view without becoming lifecycle authority.
- [ ] The final report includes exact test counts, required live skips, service targets, commit SHA, and remote parity.

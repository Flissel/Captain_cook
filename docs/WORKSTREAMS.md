# Modular delivery workstreams

This document turns the approved Captain → Hermes → Codex design into small,
mergeable branches. A branch owns one externally testable contract. No branch
may silently broaden its scope or redefine another branch's interface.

## Integration rule

`feat/devpost-demo-readiness` is the current reviewable baseline. It contains
the offline demo, evidence artifact, judge-facing docs, and release verifier.
Create subsequent worktrees from the latest approved integration branch; do
not commit feature work directly to `main`.

```text
feat/devpost-demo-readiness
        │
        ├── feat/householder-runtime-contract ── feat/householder-runtime
        │                                                    │
        ├── feat/ledger-gateway ── feat/captain-pipeline ────┼── feat/n8n-delivery ── feat/worker-fleet
        └── feat/release-evidence ───────────────────────────┘                                │
                                                                                          feat/demo-polish
```

`feat/householder-runtime-contract` defines typed role manifests and the
executor seam without an external model. `feat/householder-runtime` proves the
four roles in the real in-memory event/ledger lifecycle with deterministic
executors. `feat/captain-pipeline` consumes gateway schemas but can use a fake
`LedgerClient` in unit tests. `feat/n8n-delivery` consumes the assertion
vocabulary and adapter contracts. `feat/worker-fleet` begins only after one
gateway-backed, single-worker end-to-end run is green.

## Branch contracts

| Branch | Owner role | Produces | Must prove before merge |
| --- | --- | --- | --- |
| `feat/householder-runtime-contract` | Architect | Typed role manifest, permitted-tool policy, executor protocol, and role-result schema | Every role definition maps to exactly one constrained runtime contract and unregistered tags fail at boot |
| `feat/householder-runtime` | Delivery Builder | `HouseholderWorker`, factory injection into the existing pipeline, and deterministic offline executors | Four tagged subproblems complete through the real recorder without live model, MCP, or deployment claims |
| `feat/ledger-gateway` | Ledger Steward | MariaDB storage, FastAPI sole-writer gateway, claim fencing, validation schemas | Concurrent claim fencing and terminal-state rejection against a MariaDB test container |
| `feat/captain-pipeline` | Architect | `LedgerClient`, aligned/enriched batches, deterministic capability reuse | Every subtask belongs to exactly one batch and emitted bundles validate against the gateway contract |
| `feat/n8n-delivery` | Delivery Builder | n8n adapter, templates, deployment/observation and validation harness | One workflow deploys idempotently, runs a case, and returns evidence from a live local n8n/Mailpit stack |
| `feat/worker-fleet` | Delivery Builder | Hermes worker skill, provisioning, heartbeat and resume loop | One worker claims, builds, validates, and finalizes exactly one fenced batch without operator input |
| `feat/release-evidence` | Quality Warden | Demo sandbox, release verifier, Devpost assets, reproducibility checks | A clean clone can inspect evidence and complete the documented demo path without rebuilding every dependency |
| `feat/demo-polish` | Quality Warden | Recording captures, copy review, public-repo audit | Video, README, and submission checklist match actual commands and no credential or unimplemented claim appears |

## Householder model

The role definitions in `agents/household/` are portable sub-agent prompts.
They are not magically registered runtime workers: each is an accountable
engineering role that can be invoked by a Codex/Claude-compatible agent host
or copied into an agent task. Runtime workers are added only by their owning
feature branch after their interface and tests exist.

| Role | Owns | May not do |
| --- | --- | --- |
| Architect | Interfaces, schemas, task decomposition, dependency DAG | Add persistence or deployment behavior without the owning steward/builder contract |
| Ledger Steward | Ledger storage, gateway, fencing, state invariants | Alter worker prompts or UI copy to bypass a ledger invariant |
| Delivery Builder | n8n/Hermes/Codex execution adapters and validation evidence | Declare success from mocked deployment evidence |
| Quality Warden | Tests, reproducibility, docs, release evidence and claims audit | Expand product scope or replace acceptance criteria unilaterally |

## Working protocol for every branch

1. Copy the relevant interface from the design spec into a branch-local plan.
2. Add a failing acceptance test before implementation.
3. Keep environment-specific URLs, tokens, and credentials in `.env`; never
   add them to source, fixtures, artifacts, commits, or agent prompts.
4. Run focused tests, then `python -m pytest -q`, before a Conventional Commit.
5. Update the owning agent's handoff section with evidence, known limits, and
   the exact next dependency.
6. Merge only after the Quality Warden confirms the public README and demo
   claims still match the resulting behavior.

## Current next branch

Start `feat/householder-runtime-contract` now. It is deliberately offline and
does not depend on MariaDB, n8n, Hermes, Codex CLI, or a real API key. Create
`feat/householder-runtime` only after its manifest/executor contract is
reviewed. Start `feat/ledger-gateway` after a local MariaDB test container is
available. Until those branches are integrated, preserve the offline demo as
the judge-facing fallback.

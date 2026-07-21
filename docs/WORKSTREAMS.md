# Modular delivery workstreams

This document turns the approved Captain → Hermes → Codex design into small,
mergeable branches. A branch owns one externally testable contract. No branch
may silently broaden its scope or redefine another branch's interface.

## Integration rule

`main` is the canonical integration baseline. The MariaDB gateway is the sole production delivery source of truth
and sole MariaDB writer. The SQLite delivery ledger is legacy-import input
only; new production delivery code talks to the gateway.

Integration order: append-only gateway contract -> Captain/delivery clients ->
migration and operations -> MariaDB CI gate -> public documentation.

The offline demo, evidence artifact, judge-facing docs, and release verifier
remain the reviewable fallback. Create subsequent worktrees from the latest
approved integration branch; do not commit feature work directly to `main`.

### Hermes skill-evaluation ownership

Captain/MariaDB owns the Factory job, lease, skill release, evaluation
validation, shared-skill publication, and `ready_to_use` promotion. Hermes is
a leased worker: it uses one released skill, records build/test evidence and
may retain a private candidate, but it cannot publish or promote. Required
`TODO_TOOL.v1` gaps block Captain promotion; optional gaps remain evidence.
Minibook receives only the resulting read-only projection. The `hermes-agent/`
submodule never writes the shared registry. n8n capabilities are limited to a
Captain-issued `integration_intent=n8n` tool-integrator lease and opaque MCP
references.

### Package-C capability outcome release

`codex/package-c-capability-outcomes` owns the final composition and live gate,
not the underlying Package-A/B contracts. Its deterministic acceptance test
runs the real parser, compiler, Factory state/policy, independent package
validator, Gateway repository/catalog, execution binding, and Minibook
projector around scripted external ports. It proves restart after creation
submission and after the second normal E2E success with stable IDs and no
duplicate effects, plus committed-publication and committed-execution crash
recovery, committed-claim retry with a higher post-expiry fence,
durable-provider-effect recovery before Gateway result recording,
committed-result recovery without provider re-execution, catalog reuse without
Forge/republication, mutation, tool-gap,
holdout, post-effect deadline, artifact, and two-success failure paths. A
separately selected `live`/`db_mutating` `captain_test` test, using only an
explicitly exported DSN, exercises the production
`GatewayStore` atomic publication and
command/grant/claim/result/recovery-observation/execution transactions,
including rollback and reconstructed-store replay. Provider recovery never
rewrites `AgentRuntimeResult`; the original bytes and result ID remain the
capability-execution authority while a distinct Captain observation binds the
receipt digest, the effect-origin claim ID/fence/digest, and the active recovery
fence. Intervening expired recovery claims never replace that receipt origin.

Release evidence remains Captain-owned. Forge supplies a candidate; a Captain
Evidence Issuer supplies the controlled-recovery and normal-run records;
Captain independently validates the sealed package and derives/persists the
only terminal decision through the atomic publication method; READY is never
pre-written through the ordinary terminal API. Every invocation is persisted
before catalog resolution; a reused capability keeps a distinct invocation job
and the original release-authority job/terminal decision from the frozen
catalog record. Minibook receives only the exact successful result event and
remains a read-only rebuildable projection.
The production sandbox must be a disposable, digest-pinned Captain image with
inspectable isolation. A scripted runner is test evidence only.

The live gate runs only after deterministic and database-resetting gates. It
requires `captain_test`, reads credentials from the gitignored environment,
verifies a digest-pinned static adapter manifest without importing it, starts
only a dedicated Gateway after that side-effect-free preflight. The manifest
must bind a workspace-local Python module path, module SHA-256, and AST-proven
factory symbol. The gate health-checks
Runtime and Minibook before mutation, and does not adopt or
mutate externally owned workflow services or volumes, and treats
every missing provider, API, adapter, image, credential, skip, or projection
record as blocked. Until the production Runtime ports, capability-factory HTTP
adapters, and reviewed sandbox image exist, this branch makes no live
`ready_to_use` claim.

```text
feat/devpost-demo-readiness
        │
        ├── feat/householder-runtime-contract ── feat/householder-runtime
        │                                                    │
        ├── feat/ledger-gateway ── feat/captain-pipeline ────┼── feat/n8n-delivery ── feat/worker-fleet
        └── feat/release-evidence ───────────────────────────┘                                │
                                                                                          feat/demo-polish
```

`feat/householder-runtime-contract` defines typed role manifests, the executor
seam, and factory injection without an external model.
`feat/householder-runtime` proves the four roles in the real in-memory
event/ledger lifecycle with deterministic executors and makes that lifecycle
the offline demo. `feat/captain-pipeline` consumes gateway schemas but can use a fake
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
| `codex/agent-runtime-architecture` | Architect / Delivery Builder | Hermes-plan ingestion, Captain-owned DAG release, swarm runtime tools, scoped Codex/n8n leases, restart checkpoints, and one redacted evidence manifest | Deterministic control-plane suite plus both required real Codex/n8n live cases pass with zero skips; branch is rebased or merged only after a worktree-aware integration audit |
| `codex/package-c-capability-outcomes` | Architect / Quality Warden | Restart-safe capability-factory composition, Captain release evidence, isolated package validation, Gateway publication/execution, projection verification, and one redacted content-addressed manifest | Deterministic full chain and all negative/restart cases pass; live release remains blocked unless recovery plus three provider-backed successes, Gateway execution, and Minibook rebuild all pass against `captain_test` |

## Householder model

The role definitions in `agents/household/` are portable sub-agent prompts and
the source for constrained runtime manifests. On
`feat/householder-runtime`, each is represented by a deterministic,
in-memory `HouseholderWorker`; their reports say explicitly that no LLM, MCP
server, browser, or deployment ran. A future live executor must implement the
existing executor port and cannot silently gain routing capability from a
prompt file.

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

`feat/householder-runtime-contract` is complete and provides the reviewed
manifest/executor/factory boundary. `feat/householder-runtime` remains the
deterministic fallback path. The agent-runtime control-plane workstream now has
separate deterministic and real Codex/n8n evidence; it does not replace the
MariaDB gateway as lifecycle authority and does not claim a combined live
Minibook HTTP path. `feat/ledger-gateway` owns the remaining production
sole-writer, claim-fencing, and terminal-state gate. Preserve the offline demo
as the judge-facing fallback until every production service boundary has its own
live evidence. After an approved integration, merge into `main`, rerun all gates
there, and treat `main` as the only source of truth.

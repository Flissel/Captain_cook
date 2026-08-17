# Task 11 round-2 report — production capability bundle

Date: 2026-08-17

Branch: `codex/capability-production-bundle-d11`

Starting HEAD: `6219663`

D10 integrated HEAD: `69aff22`

Status: **offline production bundle shipped and startable**. Review Fix Round 1
closed the prior entrypoint, deep-preflight, effect-journal, and process-lifecycle
findings. This is verified offline composition and restart evidence, not a live
deployment or paid-provider result.

## Delivered

- Preserved-history merge of the independently reviewed D10 private benchmark catalog.
- Gateway lifespan composition that plans without side effects, then explicitly materializes the exact repository root, environment, private catalog, runtime, stores, and production router once before serving. `python -m gateway.app` now supplies that complete graph and serves the authenticated production health route.
- One content-addressed source authority shared by the artifact adapter and repository input store; the deep production graph rejects mixed or fake authorities.
- Explicit D10 catalog interface for open, revalidation, attestation, and materialization; durable n8n lease source injection.
- Byte-identical Gateway-issued benchmark tool evidence from the process executor through registry persistence and evaluator consumption.
- Atomic promotion ledger/projection transaction and receipt-before-return semantics for all seven effectful slices. Restart reconciliation resumes after the durable receipt without repeating the effect.
- D7 policy, price, context, provider-proxy, and USD 6 admission binding.
- Executable operator process definitions and authenticated health contracts for Gateway, provider proxy, benchmark process, runtime, Minibook, and n8n. No VibeMind volume mutation is present.
- Immutable Minibook final-evidence publication with exact PUT/readback verification before the Gateway production endpoint returns; the D12 configuration contract remains compatible.

Legacy routes remain mounted. No live service, provider, network, Docker, production database, or volume was contacted or mutated.

## RED to GREEN evidence

### Package A — lifecycle, pure preflight, source authority

RED demonstrated that the shipped application could remain on the unavailable router and that production assembly had no pure planning seam. GREEN adds deferred lifespan activation, `CapabilityAssemblyProductionPlan`, explicit materialization, and one source-artifact authority. Focused result: 105 passed.

### Package B — D10 catalog and benchmark evidence

RED demonstrated missing catalog/lease/evidence authority injection and a missing Gateway evidence issuer. GREEN binds D10 revalidation and attestation, durable n8n leases, and exact content-addressed tool evidence across 30 independently executed benchmark cases. Focused result: 174 passed.

### Package C — atomic promotion and crash reconciliation

RED demonstrated a promotion/checkpoint crash window and invalid promotion-runner stopping behavior. GREEN commits promotion ledger state and projection in one transaction and verifies receipt-before-return for scaffold, implement, seal, evaluate, promote, execute, and project. The seven-stage fresh-controller crash matrix observes no duplicate effect. Focused result: 168 passed; 12 MariaDB tests skipped because `TEST_MARIADB_DSN` was absent.

### Package D — governed process bundle and final evidence

RED demonstrated absent USD 6 rejection, incomplete D7 process bindings, and no immutable Minibook final-evidence readback. GREEN adds the admission ceiling, exact proxy/policy/context/price bindings, authenticated process health contracts, and Minibook PUT/GET equality before publication. D12 configuration compatibility: 15 passed in its independent worktree.

## Verification

- D11 production/controller/API/bootstrap/manifest: 162 passed.
- Minibook routes/projector: 53 passed.
- Neighbor capability/current/resume/catalog suites: 203 passed, one pre-existing Pydantic enum-serialization warning.
- D7 provider plus Factory repository/agent-factory suites: 54 passed, 12 MariaDB skips.
- D10/D11 integrated gate: 274 passed.
- Architecture/import/workstream gate: 18 passed.
- `compileall`: passed for `agenten`, `blockchain`, `chats`, `config`, `gateway`, and `minibook`.
- Submission verifier: passed.
- MariaDB: not run; `TEST_MARIADB_DSN` was absent, as required by the task boundary.

## Review Fix Round 1

Commits, in required integration order:

1. `f606322` — preserved-history merge of reviewed D10 HEAD `69aff22`.
2. `e7fef0a` — pure deep plan validates nested TeamExecution ports plus exact D7 URL, policy, price, context, executable, code, paid-command, and hard-ceiling bindings before materialization.
3. `bf5615f` — each of scaffold, implement, seal, evaluate, promote, execute, and project writes its durable authoritative effect receipt before returning; the fresh-controller crash matrix reconciles all seven without duplicate effects.
4. `7bb163c` — shipped Gateway, isolated benchmark, and provider-proxy module entrypoints plus the real digest-verified `CapabilityAssemblyFactoryRuntime` constructor.
5. `384ea25` — separate semantic start, stop, and restart operations for all six processes, with restart identity rotation and exact authenticated-health credential bindings.
6. `f6e711c` — verify the kernel-owned executable/start identity before stopping a managed PID and preserve configured portal adapters in the shipped production app.

Round-1 RED evidence:

- The global Gateway app could never set `production_requested`; `main()` only replaced settings on the unavailable import-time app.
- Deep validation accepted substituted nested TeamExecution authorities and did not bind five D7 authority digests.
- Controller receipts were written after adapter return, leaving a seven-slice crash window.
- Benchmark had no runnable module entrypoint; provider proxy had no module guard.
- Runtime, provider proxy, Minibook, and n8n reused `start` as `restart`; no binding exposed `stop`; benchmark referenced the legacy Gateway service action.

Round-1 GREEN evidence:

- P1 deep-plan/D7 focused gate: 146 passed.
- P2 receipt/reconciliation gates: 60 controller plus 86 production/bootstrap passed; MariaDB 2 skipped because `TEST_MARIADB_DSN` was not configured.
- P3 shipped-entrypoint gate: 33 passed; 16 Gateway-auth MariaDB-backed tests skipped in that focused selection.
- P4 USD 6, final-evidence, immutable Minibook, API, and process-manifest gate: 128 passed.
- Final managed-process identity and shipped portal-composition regression: 33 passed.
- Fresh D1/D4-D8/D10-D12 neighboring and D11 gate: 572 passed, 2 skipped only because `TEST_MARIADB_DSN` was not configured, with one pre-existing Pydantic enum-serialization warning.
- Fresh architecture/import/workstream gate: 19 passed. `compileall`, submission verification, full diff check, and diff-scoped credential-pattern scan (0 matches) passed.

The process manifest now maps Gateway to `gateway.app`, Runtime to its production
entrypoint, provider proxy to its policy/price/state-bound entrypoint, benchmark
to `gateway.capability_benchmark_process_app`, Minibook to its backend module,
and n8n only to Captain's `scripts/captain-n8n.ps1`. Sentinel lifecycle tests
execute all six without network access and prove idempotent start plus a distinct
restart identity. n8n authenticated health uses `X-N8N-API-KEY` and the exact
`CAPTAIN_N8N_API_KEY`; no VibeMind compose project or volume command is present.

## Non-claims and operator concerns

- This is offline production composition and contract evidence, not a live deployment or paid-provider proof.
- The operator must supply the documented credential environment and reachable component processes; no secret values are committed.
- n8n remains externally owned. This work neither adopts nor mutates VibeMind n8n volumes.
- D12 may consume the immutable final-evidence readback, but D12 live orchestration is not claimed by this task.

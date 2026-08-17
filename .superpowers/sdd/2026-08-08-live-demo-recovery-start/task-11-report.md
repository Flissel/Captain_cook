# Task 11 round-2 report — production capability bundle

Date: 2026-08-17

Branch: `codex/capability-production-bundle-d11`

Starting HEAD: `6219663`

D10 integrated HEAD: `69aff22`

Status: **not yet shippable**. The package gates below are green, but the final
entrypoint audit found that `python -m gateway.app` constructs the module-level
application without the production environment, runtime, private catalog, or
repository-root dependencies. Consequently `production_requested` remains
false for the shipped module entrypoint unless another caller injects those
dependencies. This does not satisfy Task 11 criterion 1 and must not be treated
as production-complete.

## Delivered

- Preserved-history merge of the independently reviewed D10 private benchmark catalog.
- Gateway lifespan composition that, when given all production dependencies, plans without side effects, then explicitly materializes the exact repository root, environment, private catalog, runtime, stores, and production router once before serving. The shipped module entrypoint does not yet supply those dependencies.
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

## Non-claims and operator concerns

- This is offline production composition and contract evidence, not a live deployment or paid-provider proof.
- The operator must supply the documented credential environment and reachable component processes; no secret values are committed.
- n8n remains externally owned. This work neither adopts nor mutates VibeMind n8n volumes.
- D12 may consume the immutable final-evidence readback, but D12 live orchestration is not claimed by this task.

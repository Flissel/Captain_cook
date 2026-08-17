# Task 9 implementer report — Hermetic four-agent Factory V3 proof

## Scope delivered

- Froze the authoritative `sales_pipeline_brief` compiler output: four ordered agents, three ordered handoffs, two required tools, one optional tool gap, and deterministic public assertion IDs are now part of the compiled specification digest.
- Added a test-only deterministic Hermes/Codex adapter behind the production worker port. It is the only writer of one isolated build workspace, creates nonempty Python source/tests, and runs the authorized compile and pytest command shapes.
- Added a hermetic E2E harness across the real Captain dispatcher/coordinator, FastAPI HTTP boundary, Minibook SQLite scheduler/store, filesystem CAS and package sealer, Gateway Action Inbox service, and Captain independent readback validator.
- Proved exactly one creation submission, one Codex session, and one immutable build effect for the exact four-agent happy path.
- Proved durable credential pause/resume across a reopened Action Inbox repository using only an opaque `credential://` reference.
- Proved `one_agent`, `null_output`, `timeout`, `receipt_only`, and `tampered_cas` fail closed with no Captain success event.

## TDD evidence

- RED: the compiler fixture failed because `CompiledFactorySpecification` did not expose the authoritative V3 agents, handoffs, tools, optional gaps, or public assertions.
- RED: the E2E initially failed because the Factory V3 harness did not exist.
- RED: Action Inbox pause/resume advanced the durable version and made the unchanged build assignment unclaimable.
- RED: replaying an already-persisted `awaiting_build` yield was treated as an invalid transition.
- RED: the authenticated worker rejected Minibook's typed `CreationProgressV1(status="validating")` effect acknowledgement as an invalid build effect.
- RED: the production runner attempted the illegal transition `validating -> succeeded`, skipping `packaging` and `readback`.
- RED: Captain compared pre-package effect URIs with Minibook's canonical republished package URIs instead of binding their digest/media identity and then verifying the canonical package.
- RED: the final candidate lacked the immutable source-archive binding because that self-reference cannot exist inside the source ZIP's pre-seal candidate.
- GREEN: each missing boundary was closed with a focused regression, then the exact E2E reached six passing cases.

## Production boundary fixes exercised by the proof

- Durable assignment claims/effects tolerate version advancement only when Action Inbox restores the exact lifecycle state and immutable assignment identity.
- Persisted yields replay idempotently without adding a new checkpoint version.
- Worker effect submission accepts only a fully bound `validating` progress acknowledgement or the legacy typed effect response; malformed or conflicting responses remain fail closed.
- V3 pipeline step outcomes explicitly advance through `packaging` and `readback` before success.
- Minibook adds `source_archive_ref` only to the final candidate; Captain reconstructs and verifies the source ZIP's pre-seal candidate without the impossible self-reference.
- Captain permits Minibook CAS namespace republishing only when digest and media type remain identical, then independently verifies the exact result refs, canonical package, ZIP contents, typed receipts, and readback sequence.

## Verification

- Exact E2E: `6 passed`.
- Focused Root regressions: `154 passed, 3 skipped`.
- Focused Minibook contracts/state/API/package/runner regressions, run with Minibook's own top-level `tests` package: `165 passed`.
- Portal full test suite: `47 passed` across 5 files.
- Portal lint: passed.
- Portal production build: passed.
- Architecture/import/workstream gates: `18 passed`.
- Python compileall across Root, Gateway, Minibook, and tests: passed.
- Ruff check over the newly added harness/E2E and the touched normally formatted Root/Minibook files: passed.
- `git diff --check`: passed.

## Authority and security ruling

- Captain remains lifecycle and success authority.
- Hermes orchestrates one authorized Codex build session; the deterministic adapter is test-only and is not a production fallback.
- Codex remains the sole isolated build-workspace writer.
- Minibook remains durable owner of the creation job, checkpoints, assignment/effect, package, CAS, and readback receipt.
- Receipt-only, null, timeout, topology drift, and tampered CAS evidence cannot produce Captain success.
- No raw credential value crosses the Action Inbox response boundary.

## Non-claims

- No live Hermes or live Codex model execution was performed or claimed.
- No Task 10 evidence was produced.
- No OpenFang action, promotion, deployment, push, merge, or external-service mutation was performed.

## Fix round 1 — authorized hermetic flow

### Review findings closed

- Replaced the former all-in-one deterministic adapter with a test-only external-model generator plus an exact Captain executor composed through `CaptainV3CodexRequestMapper`, `CaptainCodexAssignmentBuilder`, `CaptainCodexBuildSealer`, `CaptainCodexBuildReceiptIssuer`, and the Captain build CAS.
- The executor accepts and records only the assignment's two canonical argv values: `python -m compileall src` and `python -m pytest`. No absolute interpreter, basename rewriting, extra `-q`, shell launcher, or additional command is used.
- Removed all adapter-authored Captain receipts, synthetic Captain evidence, broad exception swallowing, synthetic `finish`, and unconditional failed-result branches.
- Generated source now implements and executes the exact ordered four-stage handoff (`account_researcher` → `opportunity_qualifier` → `deal_strategist` → `executive_brief_writer`). The generated real-case test writes execution evidence; Captain independently checks the trace, all public assertion IDs, both required tools, and the resulting executive brief before sealing.
- Credential demand now originates from the compiled required integration and its credential alias. Minibook durably pauses before any Codex session, the resumed opaque reference is released to the authorized executor, and the account reader requires and records its use. No credential means no build.
- `one_agent`, `null_output`, and `timeout` terminate through durable Hermes worker checkpoints plus an explicit Minibook blocked transition. `receipt_only` terminates through the real Minibook scheduler failure path. `tampered_cas` mutates the persisted ZIP and proves independent Captain readback rejection after Minibook success. Every variant persists one failed Captain agent-code evidence block with an exact failure artifact and produces zero Captain success blocks.

### RED → GREEN evidence

- RED: the expanded happy-path proof failed because the harness exposed no Minibook pause state, canonical executed argv, handoff trace, verified assertion IDs, Captain-sealed receipt readback, or build-side credential consumption.
- RED: all five negative variants returned only a synthetic `failed`/`blocked` label and exposed no persisted Minibook/Captain status, exact reason, or actual tamper evidence.
- GREEN: the exact Task-9 E2E passes all six cases with real command execution, generated real-case execution, durable negative state, and independent readback evidence.

### Fix-round verification

- Exact Task-9 E2E: `6 passed`.
- Required focused Root regressions: `116 passed, 3 skipped`.
- Full isolated Minibook non-live suite: `259 passed, 1 deselected`.
- Portal tests: `47 passed`; lint and production build passed.
- Architecture/import/workstream: `18 passed`.
- Python compileall and `git diff --check`: passed.
- Additional full Root attempt: `1917 passed, 132 skipped`; 11 readiness/fixture tests failed only because this worktree's pinned `hermes-agent` submodule is uninitialized. The required Task-9 Root gate above is green, and no submodule mutation was performed.

## Fix round 2 — production V3 authority paths

### Review findings closed

- Removed the test-only Captain executor and receipt/evidence issuers. The only remaining test double is the deterministic external-model workspace generator; exact execution, semantic assertion verification, package evidence, final receipt issuance, CAS readback, Action request coordination, and worker-failure projection now live under `agenten/agent_factory/` as injectable production components.
- The offline Captain executor enforces the assignment's exact two canonical argv values, materialized authorized roots, file and provider-cost limits, and `network_enabled=False`. It launches children with a minimal explicit environment containing system launch basics and the one approved opaque credential reference; unrelated host variables are not copied.
- Removed public assertion ID self-copying from generated source/tests. Captain now maps each frozen compiled assertion source to actual observed topology, ordered handoffs, required tool schemas/tests/results, real-case outputs, optional-tool available/unavailable policy, opaque credential use, and an attempted-but-blocked external write. Only individually proven IDs enter the final evidence.
- Added a production observation/coordinator boundary for credential demand. A typed Hermes/Minibook observation is bound to the exact CreationJob V3 policy and required integration before the coordinator projects the same canonical request to durable Minibook state and the Gateway Action Inbox.
- Added production worker-failure reporting behind the Hermes worker. The reporter accepts only a persisted failed checkpoint, durably projects the exact Minibook blocked checkpoint/reason, and creates the Captain failed evidence event. The harness now triggers and reads these paths instead of directly synthesizing the one-agent/null/timeout outcomes.
- Receipt-only and tampered-CAS failures are also passed through the production boundary-failure coordinator; tampering still changes persisted CAS bytes and Captain independently rejects readback after Minibook success.

### RED → GREEN evidence

- RED: the new authority-boundary test failed collection because no production hermetic executor, package issuer, semantic verifier, Action request coordinator, or worker-failure coordinator existed.
- RED: the expanded E2E had no optional-tool policy evidence, blocked external-write counter, child-environment sentinel result, or compiled assertion-source mapping.
- GREEN: all eight focused authority/E2E cases pass. A host `FACTORY_SENTINEL_SECRET` is deliberately set for the run and is absent from the executed generated test process.

### Fix-round verification

- Exact Task-9 E2E plus authority boundaries: `8 passed`.
- Focused Root Task-9 regressions: `124 passed, 3 skipped`.
- Full Agent Factory/Gateway non-live area: `1862 passed, 132 skipped`.
- Focused Minibook contracts/state/API/package: `123 passed`; full isolated Minibook non-live suite: `259 passed, 1 deselected`.
- Build/action/provenance/architecture/import/workstream regressions: `260 passed, 4 skipped`.
- Portal: `47 passed`; lint and production build passed.
- Ruff check/format, Python compileall, and `git diff --check`: passed.

### Non-claims

- The offline injectable executor is not composed as a live/default fallback; the Task-5 PowerShell runner ruling remains fail closed.
- No live Hermes/Codex claim, Task 10 evidence, OpenFang action, promotion, deployment, push, or merge was performed.

## Fix round 3 — sealed runtime authority and automatic projections

### Review findings closed

- Removed `CaptainHermeticBuildExecutor` and its second direct host-subprocess path. The deterministic adapter now produces only an immutable source bundle; the existing `CodexCliFactoryBuildExecutor` and `CaptainCodexBuildSealer` remain the execution and sealing path.
- Added a Captain-owned sealed static runner accepted by the existing executor's private runtime seal. The executor independently reconstructs and compares the exact workspace root, writable roots, canonical argv, `network_allowed=False`, and provider-cost cap before starting it. The runner accepts only a digest-allowlisted static source bundle, uses the approved Python interpreter for the two exact assignment argv values, and receives only the minimal explicit child environment plus the released opaque credential reference.
- Added production static workspace preparation, execution authorization, credential session, and post-execution semantic observation. The generated test executes the four ordered handoffs and writes real-case evidence; only independently verified topology, tools, policy guards, and outputs enter final package evidence.
- Moved credential-demand emission into the production Forge-submission seam. `FactoryDispatcher` invokes `CaptainFactoryCredentialActionEmitter`, which derives the missing required credential from the authoritative input and CreationJob, creates the safe checkpoint, and calls `CaptainFactoryActionRequestCoordinator`. The harness passes only the brief/configuration and observes the durable Action Inbox request; the behavioral counter proves one production emission.
- Replaced caller-supplied boundary failure codes with typed `FactoryCreationResultRejected` and `FactoryCasReadbackRejected` exceptions. `FactoryDispatcher` catches only these typed failures and invokes `CaptainFactoryDispatchFailurePropagator`; the coordinator derives the code/summary, writes durable failure evidence, and records the Captain failed block. The harness no longer calls any failure recorder.
- Timeout now travels through the existing `FactoryCodexBuildInterrupted(reason="codex_timed_out")` worker/checkpoint path and is durably projected as `codex_timeout`. Receipt-only uses the persisted Minibook terminal failure; tampered CAS mutates the addressed ZIP and is rejected by Captain's independent bounded readback.

### RED → GREEN evidence

- RED: the new authority test failed collection because `CaptainStaticFactoryRunnerFactory` and a production static build observer did not exist.
- RED: the happy path reported a Codex session before Action approval, then the existing executor rejected the fixture's non-CAS input reference. Composition was corrected so the session begins only after resume and every scaffold input is a digest-verifiable CAS object without changing the frozen assertion identities.
- RED: the timeout variant raised the existing typed interruption but was initially projected as generic `codex_failed`; the worker-failure coordinator now derives `codex_timeout` from that typed reason.
- GREEN: the exact authority/E2E suite passes nine tests, including digest rejection, one production credential emission, and one automatic failure propagation for every negative variant.

### Fix-round verification

- Exact Task-9 E2E plus authority boundaries: `9 passed`.
- Orchestration/candidate/authority/E2E focused rerun: `54 passed`.
- Root Task-9 plus Codex/provenance/architecture/import/workstream gate: `303 passed, 3 skipped`.
- Full Root Agent Factory/Gateway non-live gate: `1862 passed, 132 skipped`.
- Focused Minibook contracts/state/API/package: `123 passed`; full isolated Minibook non-live suite: `259 passed, 1 deselected`.
- Portal: `47 passed`; lint and production build passed.
- Python compileall and `git diff --check`: passed. Ruff/Black were unavailable in the existing virtual environment; no package installation was performed.

### Non-claims

- The static runner is not part of default/live composition and does not weaken the Task-5 PowerShell live ruling.
- No live Hermes/Codex claim, Task 10 evidence, OpenFang action, promotion, deployment, push, merge, or external-service mutation was performed.

## Fix round 4 — technically enforced network isolation

### Review finding closed

- Removed the sealed static runner's normal host-Python subprocess execution. The runner no longer accepts a caller-owned expected digest or interpreter path as execution authority.
- Added a Captain-owned, step- and call-depth-bounded restricted Python engine. It parses every materialized Python file before execution and permits only an explicit AST subset. Imports, attributes, dynamic call targets, loops, exception machinery, comprehensions, process access, network access, and general filesystem/environment primitives are absent and denied by default.
- The two canonical command shapes retain real semantics inside the restricted engine: the compile step parses and validates every source/test module, and the test step executes generated functions and assertions. The only writable primitive is a Captain-fixed real-case evidence sink under the authorized evidence root; external-write attempts are represented by a Captain primitive that only increments its blocked counter.
- Reworked the deterministic external-model bundle to emit generated Python source and a generated test compatible with the restricted subset. The four-agent handoff, required tools, optional-tool policy, opaque credential use, blocked external write, and executive brief are still executed and independently checked by the production observer.

### RED → GREEN evidence

- RED: the adversarial bundle contains a real `socket.create_connection` attempt. A raise-on-call counter on host process launch showed the desired restricted runner API did not exist: construction failed because the old runner still required caller-owned digest and interpreter parameters.
- GREEN: the same adversarial test is rejected as forbidden restricted Python before any host-process call (`host_process_calls == 0`). The valid generated source/test then passes through the exact Task-9 flow without a subprocess.

### Fix-round verification

- Adversarial network boundary: `1 passed`.
- Exact Task-9 authority and E2E cases: `9 passed`.
- Focused Root Task-9 regressions: `77 passed, 3 skipped` (MariaDB-gated skips). The plan's historical `test_forge_contract_compatibility.py` path is absent, so the extant listed Root files plus authority/E2E were run.
- Full Root Agent Factory/Gateway non-live area: `1863 passed, 132 skipped`.
- Focused Minibook contracts/state/API/package: `123 passed`; full isolated Minibook non-live suite: `259 passed, 1 deselected`.
- Portal: `47 passed`; lint and production build passed.
- Architecture/import/workstream: `18 passed`.
- Python compileall and `git diff --check`: passed. Ruff/Black remain unavailable in the existing virtual environment; no dependency installation was performed.

### Non-claims

- The restricted static engine remains an injected hermetic Task-9 adapter path, not default/live composition and not a Task-10 runtime.
- No host firewall mutation, Docker/live service, live Hermes/Codex claim, OpenFang action, promotion, deployment, push, or merge was performed.

## Fix round 5 — bounded validation and Captain-derived execution evidence

### Review findings closed

- Bound the sealed static source bundle to the exact assignment `max_files` before decoding, parsing, traversing, or materializing it. Hard package ceilings now additionally cover total files, source/test counts, ASCII-safe relative paths, path length, individual source/archive bytes, and aggregate bytes.
- Added bounded UTF-8/token validation before `ast.parse`, including token, string-literal, bracket, and indentation limits. The iterative Captain AST validator now checks the deadline on every node and enforces node-count, depth, integer, literal, container-size, and fanout limits without `ast.walk`.
- Propagated the exact Captain deadline into the sealed runner and checks it during bundle preflight, lexical validation, AST traversal, evaluation, function calls, materialization, and evidence writes. Expiry is typed and fail closed.
- Removed the generated `emit_evidence` primitive and all generated truth-payload authority. Generated tests execute the actual source entrypoint, then Captain resets all observations and invokes the fixed `run_sales_pipeline` entrypoint itself with Captain-owned input.
- Restricted execution now exposes only narrow observed primitives for the opaque credential accessor, required account/scoring tools, optional industry tool, ordered handoff recording, and denied external writes. Captain alone derives the trace, tool results, optional policy, credential use, business result, blocked-write counters, and assertion input evidence from those calls and the entrypoint return value.
- The adversarial bundle whose source returns `WRONG` while its generated test asserts true and submits a fabricated success payload is rejected before either command evidence or real-case success evidence exists.

### RED → GREEN evidence

- RED: the seven initial adversarial cases failed because runtime authority had no assignment-bound file count, unsafe Unicode/long paths were accepted, validation materialized before resource/deadline rejection, and generated `emit_evidence` could still submit a fabricated truth payload.
- GREEN: assignment file-count, oversized source, deep syntax, oversized literal, invalid UTF-8, AST depth/container abuse, Unicode/long paths, incremental deadline expiry, and fabricated evidence all fail typed before success. The focused authority file passes 14 tests.
- GREEN: the exact four-agent happy path and all five negative variants remain fail closed through the full Task-9 authority/E2E set (`20 passed`).

### Fix-round verification

- Exact Task-9 authority and E2E: `20 passed`.
- Focused Root Task-9 regressions: `93 passed, 3 skipped` (MariaDB-gated skips).
- Full Root Agent Factory/Gateway non-live area: `1874 passed, 132 skipped`.
- Full isolated Minibook non-live suite, run in a separate process with Minibook's top-level `tests` package: `259 passed, 1 deselected`.
- Portal: `47 passed`; lint and production build passed.
- Architecture/import/workstream: `18 passed`.
- Python compileall and `git diff --check`: passed. Ruff remains unavailable in the existing virtual environment; no dependency installation was performed.

### Non-claims

- The restricted static engine remains an injected hermetic Task-9 path, not default/live composition and not a Task-10 runtime.
- No live Hermes/Codex evidence, OpenFang action, promotion, deployment, push, merge, or external-service mutation was performed.

## Task 9A — isolated source and test namespaces

### Critical finding closed

- Restricted source now executes in one seal-once, read-only namespace. Every generated source function is bound to that original namespace instead of consulting mutable engine state at call time.
- Captain captures the packaged `run_sales_pipeline` function after all source modules load and before any test module executes. The final Captain-owned real-case invocation always calls that captured source function.
- Each test module executes in its own isolated namespace. Tests receive read-only public source exports plus the bounded `len` primitive; credential, tool, handoff, external-write, and environment primitives are not exported to test code.
- Direct definitions, assignments, nested assignments, and argument bindings may not shadow a source-owned exported name. The restricted AST already excludes attribute access, subscript assignment, and callable container mutation, so aliases and container references cannot mutate the frozen source namespace.

### RED → GREEN evidence

- RED: a bundle whose packaged entrypoint returned `WRONG` was accepted when its generated test defined a successful helper and assigned it to `run_sales_pipeline`. The adversarial regression failed with `DID NOT RAISE`, proving Captain had invoked the test replacement.
- GREEN: the same bundle is rejected as typed restricted Python before command-success or real-case evidence is written. The exact valid four-agent package and all five existing fail-closed variants remain green.

### Verification

- Task-9 authority boundaries and exact E2E: `21 passed`.
- Focused Root contracts/compiler/orchestration/Minibook Forge/Hermes worker/Gateway API: `121 passed, 3 skipped` (MariaDB-gated skips).
- Focused isolated Minibook contracts/state/API/package: `123 passed`.
- Portal: `47 passed`; lint and production build passed.
- Architecture/import/workstream: `18 passed`.
- Root Python compileall and `git diff --check`: passed.

### Non-claims

- No live Hermes/Codex evidence, Task-10 execution, OpenFang action, promotion, deployment, push, merge, or external-service mutation was performed.

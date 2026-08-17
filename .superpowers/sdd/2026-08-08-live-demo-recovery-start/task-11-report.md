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

## Review Fix Round 2

Commits:

1. `7bdbc2c` — preserved-history merge of reviewed D12 HEAD `38f9455`.
2. `dac6be6` — pure, digest-attested Factory runtime descriptor validates exact constructor, nested port types/signatures, roots, clock, model client, and authority identities before any materialization; the constructor receives only an allowlisted non-secret configuration object.
3. `e31260a` — each paid resume command and Gateway authority is bound to the exact D7 tuple: proxy URL, policy, price, command context, executable, code, paid-command, and `hard_ceiling_enforced=true`, plus session/result identity. The production manifest carries the same tuple.
4. `2133ca8` — D12 consumes typed D11 start/stop/restart and authenticated-health controls for all six services. Runtime restart is executable; process state carries typed identity kind/value; n8n uses verified Docker container ID plus start identity, authenticated `X-N8N-API-KEY` health, bounded stop, and exact-container kill fallback.

Round-2 RED to GREEN evidence:

- Group A: wrong constructor/model/clock/nested-port bindings previously reached materialization. GREEN focused gate: 100 passed; bootstrap gate: 32 passed.
- Group B: the strict command contract rejected the four missing D7 fields (9 targeted RED failures). GREEN focused gate: 173 passed; every eight-field drift is rejected before the paid Codex effect.
- Group C: D12 still returned unconditional unavailable controls and stale D10/D11 absence assertions. GREEN D11 manifest/operator plus complete D12 orchestrator gate: 65 passed. The sentinel consumer executes all six exact semantic controls and restart produces a new identity.

Round-2 verification:

- Offline repository gate with `tests/live` excluded explicitly: 3,613 passed, 137 skipped, 11 failed solely because the `hermes-agent` submodule is uninitialized in this worktree (missing pinned runtime surfaces/fixture copies). One pre-existing Pydantic serialization warning was emitted.
- The ordinary `-m "not live"` command cannot collect three files under `tests/live` because the uninitialized Hermes submodule lacks `utils` and `hermes_cli`; no live test or service was run.
- Architecture/import/workstream: 18 passed.
- Submission verifier, `compileall`, full round diff check, and credential-pattern scan: passed; credential matches: 0.
- MariaDB: not run because `TEST_MARIADB_DSN` was absent.

Round-2 non-claims:

- No live provider, network service, Docker engine, n8n deployment, volume, or production database was contacted or mutated.
- Authenticated health and container identity behavior is covered offline by injected/sentinel boundaries; this is not a live deployment claim.
- The uninitialized Hermes submodule prevents a repository-wide all-green claim, but does not affect the focused D11/D12 gates above.

## Review Fix Round 3

Commits:

1. `4bdf152` — ships the repository-owned Factory adapter and deterministic descriptor generator. Planning single-opens and HEAD-attests both descriptor and module, validates an exact fail-closed AST and constructor signature, and builds the complete deferred sole-writer graph without importing adapter code or constructing persistence, clients, directories, or processes. Explicit materialization binds the same authorities and revalidates their identities.
2. `3349493` — isolates each managed service environment to an explicit allowlist and its own credentials. Existing processes are reused only after exact kernel or Docker identity plus authenticated health; n8n persists `process_id=null` with exact container ID and `StartedAt`. Start/health failure performs bounded, identity-scoped process-tree or container cleanup and retains ambiguous running state.
3. `d706ebb` — makes D12 status and paid preflight consume all six exact D11 authenticated-health URL/header/credential contracts. Minibook uses `/api/v1/projection-health`; n8n uses `X-N8N-API-KEY`. Any missing, wrong, timed-out, or non-ready authority stops before a paid Gateway POST, advance, or provider effect.

Round-3 RED to GREEN evidence:

- Group A: operator-authored descriptors, same-hash descriptor drift, invalid signatures/top-level calls, and deep nested fake bindings were rejected before any constructor or filesystem effect. The shipped adapter/descriptor/deferred-graph gate passed 104 tests; the committed descriptor was then opened against the real HEAD successfully.
- Group B: Windows exposed the expected cleanup RED because `SIGKILL` is unavailable. The GREEN implementation uses exact identity checks, bounded TERM plus Windows process-tree fallback, bounded Docker/Pwsh operations, and durable ambiguity retention. Manifest/process security gate: 8 passed.
- Group C: the prior consumer trusted process state and omitted n8n from paid health. GREEN validates all six semantic contracts for both `status` and paid `run`/`resume`; each individual authority-failure case proves zero POST/advance. Focused D12 gate: 43 passed; combined D11/D12 gate: 202 passed.

Round-3 verification:

- D1/D4-D12 and prior critical capability/Factory/provider/demo regression selection: 1,940 passed, 2 skipped, with one pre-existing Pydantic serialization warning.
- Full offline repository gate with `tests/live` explicitly excluded: 3,625 passed, 137 skipped, 14 failed. Eleven failures are the already documented uninitialized `hermes-agent` fixture/runtime surfaces. Three `tests/agent_runtime/test_generate_runtime_adapter_manifest.py` failures require a worktree-local `.venv/Scripts/python.exe`; this worktree intentionally uses the repository-root virtual environment and no local environment was created. These are environment non-claims, not an all-green repository claim.
- Architecture/import/workstream: 18 passed. `compileall`, submission verifier, round diff check, and credential-pattern scan passed; credential matches: 0.
- MariaDB authority gate: 14 skipped because `TEST_MARIADB_DSN` was absent. No production database was contacted.

Round-3 non-claims:

- No live network service, provider, Docker engine/container, n8n deployment/volume, or production database was contacted or mutated.
- Process identity, authenticated health, and cleanup behavior are proven through offline injected/sentinel boundaries; this is not a live deployment claim.
- The canonical deferred Factory ports are production graph authorities, but external Forge and benchmark-input provisioning remains an operator prerequisite before real paid dispatch.

## Review Fix Round 4

Commits:

1. `1e8dcbe` — replaces generic environment stripping with explicit real-entrypoint contracts. Only Provider Proxy receives `OPENAI_API_KEY` and the provider-receipt/runtime secrets; Runtime receives its Gateway, adapter, and proxy-client bindings; Gateway receives the exact capability/catalog/provider/Minibook configuration without the OpenAI/provider-receipt secrets.
2. `c81904f` — extends the Factory descriptor across the real internal implementation path, source digest, AST digest, and HEAD blob. Adapter and implementation bytes are single-open/HEAD-attested before effects and executed from those byte snapshots in isolated module namespaces; the wrapper no longer imports the internal implementation.
3. `8aff972` — replaces the direct OpenAI Factory model path with an AutoGen `ChatCompletionClient` over the proxy's exact `/v1/responses` surface. The builder binds loopback URL, proxy credential, Gateway context reference/header, all six D7 digests, and `hard_ceiling_enforced=true`; it maps Responses text/function calls and usage without exposing an OpenAI key to Gateway.
4. `46f40f7` — installs the concrete Minibook Forge HTTP/mapper/Gateway-evidence graph and a Gateway/D10-backed benchmark-input readiness gate. The deferred Forge/input classes and in-memory benchmark repository are absent from the production implementation.
5. `8428946` — persists `starting` before every OS/container spawn, persists the PID before its identity probe, retains ambiguous start state on timeout or missing identity, snapshots exact parent/descendant kernel identities before TERM, and performs bounded per-identity tree cleanup with confirmed exit.

Round-4 RED to GREEN evidence:

- Group A: filtered real settings previously failed because Runtime/Gateway/Benchmark authorities were missing and the provider receipt name was wrong. GREEN service-manifest gate: 9 passed.
- Group B: the descriptor previously trusted only a wrapper that imported an unverified cached module. GREEN bootstrap/production graph gate: 71 passed; dirty/same-size and AST/signature/top-level drift remain fail-closed before construction.
- Group C: the production graph previously constructed `OpenAIChatCompletionClient` with a direct provider key. GREEN proxy/Factory gate: 83 passed; endpoint, bearer, exact context reference, usage mapping, and zero direct OpenAI authority are asserted.
- Group D: production contained `_DeferredForge`, `_DeferredBenchmarkInputs`, and `InMemoryBusinessBenchmarkRepository`. GREEN graph/orchestration gate: 70 passed with concrete Minibook and Gateway/D10 types.
- Group E: no durable state existed before spawn and Windows cleanup knew only the parent PID. GREEN lifecycle/D12 gate: 81 passed, including pre-spawn crash, missing identity, n8n timeout, and orphan-child cases.

Round-4 verification:

- D1/D4-D12, prior critical capability, Factory, provider-proxy, and demo regressions: 1,946 passed, 2 skipped, with one pre-existing Pydantic serialization warning.
- Architecture/import/workstream: 18 passed. `compileall`, submission verifier, round diff check, and credential-pattern scan passed; credential matches: 0.
- MariaDB authority gate: 14 skipped because `TEST_MARIADB_DSN` was absent. No production database was contacted.

Round-4 non-claims:

- No live service, network request, provider, Docker engine/container, n8n deployment/volume, or database was contacted or mutated.
- The Gateway/D10 benchmark input port deliberately returns `incomplete` until exact job-specific dispatch inputs exist; this is not a paid benchmark-ready claim.
- The full repository gate was not repeated in Round 4; Round 3's Hermes/worktree-environment failures remain the repository-wide non-claim. The changed D1/D4-D12 neighborhood is green as recorded above.

## Review Fix Round 5

Commits:

1. `0cac64f` — canonicalizes the shared receipt authority as
   `CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN`, limits it to Gateway and Provider
   Proxy, passes only the n8n allowlist to every Docker/Pwsh operation, and
   rejects non-loopback or route-drifted health URLs before attaching a token or
   issuing a request.
2. `7eb4fc5` — generates and attests the complete repository-local transitive
   Factory import closure (102 modules), including source, AST, HEAD blob,
   revision, and configuration digests, before any persistent constructor. A
   scoped verified-byte finder/loader supplies the attested closure rather than
   trusting disk import bytes.
3. `0d4ae79` — completes the proxy-bound Responses AutoGen client: fixed high
   reasoning, text and function-call parsing, assistant/function-result input
   serialization, tools, JSON text formats, usage, and cancellation, with no
   retry or direct OpenAI endpoint.
4. `05550bf` — supplies the real Minibook creation/projection configuration and
   removes the incompatible profile-bound Business Benchmark dependency from
   the generic capability runtime. The reviewed generic assembly benchmark port
   remains the sole benchmark authority and the legacy dispatcher fails closed.
5. `1558699` — rejects substituted preloaded closure objects even when they
   claim the expected file path.
6. `785b9e1` — makes `starting` an exclusive durable claim. A subsequent start
   reconciles the exact process/container and authenticated health before doing
   anything; missing or ambiguous identity never spawns again. Failed process
   tree enumeration is ambiguous and cannot be reported as stopped.
7. `f8710e1` — fingerprints every source-declared function retained by a preloaded
   closure module against the already-attested bytes. A real `ModuleType` with
   the expected origin but substituted code is rejected before construction.

Round-5 RED to GREEN evidence:

- Group A authentication/environment and URL gate: 36 passed. Gateway and
  Provider Proxy share exactly one receipt-token name; all other service
  environments exclude it. Health URL validation proves zero request for an
  attacker host or path drift.
- Group B closure gate: 63 passed after the initial transitive/import-cache
  adversarial cases. The additional same-origin non-module and same-origin code
  substitution tests were each RED then GREEN. The complete post-hardening
  bootstrap/production selection passed 146 tests.
- Group C Responses/D7 contract gate: 24 passed against the D7 proxy transport,
  including dynamic command context, eight-field authority headers, function
  calls, JSON output, usage, and cancellation.
- Group D concrete Factory/readiness gate: 141 passed. Generic first-dispatch
  reaches the reviewed assembly benchmark authority; the incompatible legacy
  profile contract is not presented as generic-ready.
- Group E lifecycle adversarial gate: two new tests failed first, then the
  complete manifest selection passed 18 tests. n8n timeout and incomplete OS
  identity retain `starting`; a second start counter remains zero; process-tree
  enumeration failure retains ambiguity.

Round-5 verification:

- D1/D4-D12 capability, Factory, provider-proxy, and demo neighborhood: 2,339
  passed, 130 skipped; the skips are environment/live authorities excluded by
  `-m "not live"`. One previously documented Pydantic enum-serialization warning
  remains.
- Minibook offline gate: 131 passed, one live test deselected.
- Architecture/import/workstream: 18 passed.
- `compileall`, submission verifier, full `f4fd254..HEAD` diff check, and
  diff-scoped credential-pattern scan passed; credential matches: 0.
- MariaDB was not run because `TEST_MARIADB_DSN` was absent. No database was
  contacted.

Round-5 non-claims:

- No live network service, paid provider, Docker engine/container, n8n
  deployment/volume, or database was contacted or mutated.
- These gates establish offline production composition, authority validation,
  and injected/sentinel lifecycle behavior. They do not claim a live deployment
  or paid benchmark execution.
- The broad suite must keep Minibook tests in a separate pytest invocation
  because both roots contain a Python package named `tests`; combining the roots
  causes collection namespace collisions. Both corrected invocations are green
  above.

## Final Review Fix Round 6

Commits:

1. `5f2aecd` — serializes cross-process service starts under an atomic durable
   claim and validates the sealed health/config contract before state or spawn.
2. `eb451e6` — rejects a Windows CIM process tree when any enumerated child
   lacks a readable exact kernel identity; ambiguous cleanup retains state.
3. `97f0f75` — protects every Minibook Creation route with the exact Minibook
   bearer authority and requires the matching Gateway key and creation database
   before spawn.
4. `b00a9e5`, `c9331a3`, and `cd569a4` — complete relative/package import
   closure generation, attest every source/AST/HEAD binding, and materialize the
   real graph only from verified bytes without a `sys.modules` bypass.
5. `dd70936` — validates the exact `AssemblyOwnedGenericBenchmark` and
   `ProxyBoundResponsesModelClientBuilder` graph with all six D7 digests and the
   hard-ceiling binding.
6. `02d2c3f` — prevents `extra_create_args` from replacing model, input,
   reasoning, tools, output schema/format, command context, or authority headers.
7. `93f5a18` and `2e1c9d0` — bind tool evidence to durable Gateway benchmark
   WorkBatch and Runtime command/result readback, then ship the authenticated
   Gateway-owned execution API and repository-owned isolated executor. Public
   `declared_tool_keys` are compiler-authority digest bound; zero, extra, wrong,
   replayed, foreign-case/job/run evidence is rejected before issuance.
8. `e3ed64c` — materializes the passed generic D10 suite as typed immutable
   Factory V3 `QUALITY_REVIEWED` evidence: exactly 15 job-specific cases, three
   per category, 30 independent evaluation references, and the exact public
   Captain assertion set. It reaches `VALIDATE_FOR_PROMOTION` without inventing
   legacy profile, workflow-feedback, Claims, or Renewal artifacts.
9. `c50cb81` — verifies the atomic Gateway promotion replay and exact immutable
   Minibook projection readback. A changed replay conflicts and cannot append a
   second promotion.

Round-6 RED to GREEN evidence:

- Operator/Minibook/closure/validator/Responses packages were each reproduced
  by the final reviewer adversarial cases before their focused green commits.
- Gateway benchmark execution initially lacked a productive repository-owned
  issuer route. GREEN focused execution/bootstrap/process gate: 127 passed.
  The only injected fake is the offline Runtime effect boundary; counters prove
  one effect while all call/lease/receipt identities come from persisted
  Gateway WorkBatch and Runtime command/result authorities.
- The prior generic Factory benchmark deliberately raised a deferred exception.
  The new bridge tests were RED on the missing typed quality contract and
  constructor, then GREEN. The shipped Store promotion test exposed and fixed
  an invalid assumption that every Factory job UUID was version 4. Focused
  Factory/benchmark/bootstrap/production gate: 196 passed, one pre-existing
  Minibook escape-sequence warning.
- Private-leak assertions confirm the durable public summary contains no
  expected predicates, private holdout bodies/URIs, outcome answers, verdicts,
  or evaluator internals.

Round-6 verification:

- First explicit offline root run: 3,667 passed, 137 skipped, 8 deselected, and
  14 environment-only failures. Three generator tests require a missing
  worktree-local `.venv`; eleven Hermes readiness/fixture tests require the
  uninitialized `hermes-agent` submodule.
- Corrected broad D1/D4-D12/Factory/Gateway run with `tests/live`, the three
  environment-bound files above, and legacy tests explicitly excluded: 3,657
  passed, 137 skipped, 8 deselected, with five documented warnings.
- Minibook's reviewed `tests/` suite, run separately to avoid its legacy
  top-level `test_api.py` credential-file collector: 132 passed, one live test
  deselected.
- Architecture/import/workstream: 18 passed. `compileall`, submission verifier,
  full `628d149..HEAD` diff check, and diff-scoped credential scan passed;
  credential-pattern matches: 0.
- `TEST_MARIADB_DSN` was absent, so the MariaDB authority gate was not run. No
  database was contacted.

Round-6 non-claims:

- No live network service, paid provider, Docker/PowerShell process, n8n
  deployment/volume, or production/test database was contacted or mutated.
- The offline Runtime effect in the productive Gateway execution contract is an
  injected counted boundary; these tests do not claim a live Codex/provider run.
- Repository-wide all-green is not claimed because the worktree has no local
  `.venv` and its `hermes-agent` submodule is uninitialized. The explicitly
  bounded D1/D4-D12 product neighborhood and corrected broad gate are green as
  recorded above.

## Final Review Fix Round 7

Commits:

1. `141650e` — rejects preloaded closure modules whose imported module,
   function, class, origin, source, or code bindings differ from the verified
   repository closure, including relative and package imports.
2. `ca369ab` — seals each generic benchmark request to the released candidate,
   team manifest, model, execution policy, and independently built one-agent
   baseline before an execution effect can begin.
3. `9919a17` — replaces the conflicting promotion-subject encodings with one
   canonical deterministic helper used by Store emission and execution
   readback.
4. `0f617d4` — adds the digest- and HEAD-attested repository-owned public tool
   registry with exact schema, operation, endpoint, credential-env-name, and
   registry bindings.
5. `2ba0f2b` — ships the productive `AUTOGEN_BENCHMARK` path. Gateway persists
   the exact WorkBatch, tool call, and single-use lease before the host effect;
   the released team and sealed one-agent baseline execute through the real
   Factory HostAutoGen ports; terminal receipts are issued only after exact
   Runtime result and CAS readback.
6. `8c013b8` — regenerates the verified Factory dependency closure and shipped
   descriptor after the production benchmark graph changed.
7. `c128db0` — proves all public tools declared by the three shipped D11 demo
   inputs resolve to concrete local read-only or bounded authenticated HTTP
   adapters; missing operator credentials fail readiness before an effect.
8. `0c2228d` — retains the exact sealed Factory authority inside production
   composition so benchmark candidate and baseline execution cannot fall back
   to a deferred or synthetic graph.

Round-7 RED to GREEN evidence:

- Closure adversarial tests replaced imported module globals and preloaded
  functions/classes and exercised relative/package imports. All substitutions
  now fail before Factory materialization.
- Benchmark REDs showed that a team-kind label and post-effect synthetic tool
  evidence were insufficient. GREEN binds the actual released candidate/team
  and separately sealed one-agent baseline to the same public case, model, and
  policy; agent names, handoffs, messages, and tool calls come from the actual
  HostAutoGen task result.
- Zero, extra, wrong, replayed, or foreign job/case/run/tool authority is
  rejected. Every declared tool has an exact pre-effect persisted call/lease,
  one counted host effect, typed readback, and only then a terminal receipt.
  No expected predicates, private case bodies/URIs, outcomes, verdicts, or
  evaluator internals enter the executor view or durable public summary.
- The concrete registry covers Sales, Incident, and Claims inputs. Remote
  adapters use sealed loopback endpoints, exact operation/schema bindings,
  credential environment names rather than values, fixed timeouts, disabled
  redirects and environment proxies, bounded responses, sanitized errors, and
  typed readback. Operator workflow/service provisioning remains required and
  absent configuration is `incomplete`, never ready.
- Focused production Gateway/Factory/benchmark/registry/manifest tests: 404
  passed and 2 skipped after the final three composition repros passed.

Round-7 verification:

- Offline broad product gate with `tests/live` and the two unavailable Hermes
  contract files explicitly excluded: 3,682 passed, 137 skipped, with five
  documented warnings and no failures.
- An earlier unbounded collection attempt confirmed the checkout's
  `hermes-agent` submodule lacks the referenced Hermes runtime/fixture modules;
  those external surfaces are recorded as a non-claim, not converted to PASS.
- Architecture/import/workstream: 18 passed. `compileall`, submission verifier,
  and full `8342467..HEAD` diff check passed.
- Added-line credential scan found no credential-shaped assignment. A broad
  bearer-pattern scan found only the explicit synthetic test fixture
  `incident-tool-token-123`; no real credential or secret value was added.
- `TEST_MARIADB_DSN` was absent, so the MariaDB authority gate was not run. No
  database was contacted.

Round-7 non-claims:

- No live network service, paid provider, Docker/PowerShell process, n8n
  deployment/volume, or database was contacted or mutated.
- The HTTP/n8n tool adapters are executable, fail-closed operator contracts;
  this offline gate does not claim their external workflows are provisioned or
  reachable.
- Hermes collection/readiness is not claimed because the required submodule
  runtime and fixture files are absent from this checkout.

## Final Review Fix Round 8

Commits:

1. `be69d1f` — uses the actual Factory job UUID as the one canonical promotion
   subject through Store emission, execution, projection, acknowledgement, and
   immutable readback.
2. `8e8e550`, `77b0bc1`, `324897b`, and `b240151` — ship the authenticated,
   HEAD/digest-attested loopback tool host and content-addressed Sales,
   Incident, and Claims demo-fixture datasets. The host journals the exact
   typed effect result and receipt before responding and replays it after
   restart without recomputation.
3. `00efe80` and `fdef424` — move HostAutoGen exclusively into the isolated
   authenticated benchmark process. Gateway persists WorkBatch, command,
   per-case/tool call and single-use lease before the host effect, then persists
   terminal result and receipt before the response. The child dependency
   versions/origins are attested under a fresh `python -I` interpreter.
4. `545e499` — replaces the legacy profile-bound real-case path with exact
   generic D10 V3 quality evidence for all three canonical inputs. Fifteen
   job-specific cases and thirty independent evaluation refs reach
   `VALIDATE_FOR_PROMOTION` without Claims/Renewal reuse or private leakage.
5. `1ab2ef9`, `b372739`, and `73849fb` — define and ship the separate
   `FACTORY_RUNTIME` role and bounded typed Factory-to-Gateway RPC. Production
   Gateway materializes only a process client; the fresh isolated Factory child
   builds the verified closure and receives no DB, Gateway-admin, provider,
   OpenAI, or Minibook credential. Gateway remains sole writer and sole Forge
   credential holder. Every command binds job, correlation, public input,
   attempt, operation, request, idempotency key, and exact revision.
6. `8246439` — corrects the immutable-promotion behavioral fixture to use a
   valid production-shaped UUIDv4 subject; product subject encoding was not
   weakened.

Round-8 RED to GREEN evidence:

- The first Factory RPC test was RED because no repository-owned boundary
  existed. GREEN covers the distinct bearer role, operation allowlist, 1 MiB
  payload ceiling, cross-job/revision rejection, fixed redacted failures, and
  durable replay.
- The process crash test constructs a fresh app, executor, and Store client
  after the first adapter return. The restarted process reads the same durable
  Gateway receipt, returns byte-identical evidence, and the injected host
  effect counter remains exactly one.
- Production Factory execution now traverses `GatewayFactoryProcessClient` to
  `gateway.capability_factory_process_app`; repository and control mutations
  return through the authenticated Gateway sole-writer API. Forge remains a
  Gateway control effect, so the child never receives `MINIBOOK_API_KEY`.
- The operator and D12 consumer now carry seven exact semantic services,
  including Factory start/stop/restart, authenticated `/healthz`, fresh
  isolated interpreter argv, its own credential env, and no cross-service
  secrets. The shipped Factory entrypoint and graph are revision/digest/closure
  attested before materialization.
- Focused D11/D12/process/bootstrap/production gate: 252 passed, five warnings.
  Post-commit closure/process/manifest/D12 revalidation: 94 passed, four
  warnings. RPC payload/crash gate: 6 passed.

Round-8 verification:

- The first unbounded offline root run produced 3,707 passes, 137 skips, and
  12 failures. Eleven failures are exclusively the absent/uninitialized
  `hermes-agent` runtime and fixture files. The one in-scope failure was an old
  non-v4 test UUID and was corrected without changing production validation.
- Corrected offline product gate, explicitly excluding `tests/live` and only
  `tests/contracts/test_hermes_runtime_readiness.py` plus
  `tests/contracts/test_work_package_compatibility.py`: 3,701 passed, 137
  skipped, five documented warnings, zero failures.
- Minibook offline suite: 132 passed, one live test deselected.
- Architecture/import/workstream: 18 passed. `compileall`, submission verifier,
  full `8f0b303..HEAD` diff check, and added-line high-confidence credential
  scan passed; secret-pattern matches: 0.
- `TEST_MARIADB_DSN` was absent, so the MariaDB authority gate was not run. No
  database was contacted.

Round-8 non-claims:

- No live network service, paid provider, Docker/PowerShell managed service,
  n8n deployment/volume, or database was contacted or mutated.
- Tool-host datasets are explicitly demo-fixture evidence, not claims about
  production-world state. External n8n/workflow provisioning and reachability
  remain separate live evidence.
- Hermes collection/readiness remains unclaimed because this checkout does not
  contain the required initialized submodule runtime and fixture files.

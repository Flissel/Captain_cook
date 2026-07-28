# Business benchmark live-bootstrap gaps

- [x] Replace the blanket production-bundle failure with exact fail-closed
  bootstrap diagnostics before Gateway, provider, or n8n effects.
- [x] Provide concrete adapters for Gateway job/execution/budget/candidate
  projections, canonical private-suite provisioning, CAS-bound Captain policy,
  Gateway-backed evaluation/report invocations, and immutable receipt
  finalization.
- [x] Implement a durable `CaptainHumanReviewPort` backed by Captain authority.
  It must persist the exact request/effect/fence binding and return only an
  accepted or completed typed receipt. Automatic completion and in-memory
  approval are forbidden. Both canonical suites contain mandatory handoffs, so
  provider-live construction remains blocked until this port exists.
- [x] Integrate a concrete `FactoryN8nToolAdapterPort` plus
  `FactoryN8nGrantAuthorityPort` for the Renewal ordinary/boundary cases. The
  adapter must use the Captain-owned n8n MCP endpoint, validate the exact
  command/grant/workspace binding, and retain paired host/n8n evidence. It must
  not connect to or adopt VibeMind n8n.
- [ ] Add an opt-in isolated `captain_test` acceptance test that constructs the
  full default loader, performs health preflight, and proves the expected
  30-receipt single-team scope without claiming promotion or Minibook evidence.
- [ ] Replace the legacy PowerShell-compatible n8n deployment payload digest
  with one portable versioned canonical JSON algorithm, redeploy the managed
  workflow, and retain an explicit migration reader for existing v1 receipts.
- [x] Add the deployable Factory resume operator. The opt-in command now wires
  `CaptainCreationJobMapper`, one-shot `MinibookSwarmForge`, persistent creation
  CAS, strict Forge-result-to-`ResolvedFactoryCandidate` binding, the Gateway
  composition root, private technical-holdout selection, scoped host/n8n tools,
  and lazy provider-backed benchmark inputs. The PowerShell demo invokes it
  only after an unresolved preflight and a process-only provider-key check,
  validates its redacted two-job result, and repeats preflight before any full
  benchmark call.
- [ ] Add a Captain-owned retry/improvement authorization writer. The bounded
  runner must continue to stop at its typed Captain checkpoint when a failed
  evaluation needs another attempt; it must never self-authorize an
  improvement or substitute the pre-provisioned seed candidate for newly
  generated Forge code.
- [ ] Finish the Minibook creation worker. `CreationScheduler`, resumable
  checkpoints, exact one-shot result files, a persistent local CAS, a
  deterministic creation-only exporter, and Gateway CAS replay are now
  implemented. The HTTP scheduler still lacks a real pipeline factory, and a
  live Forge run remains blocked unless the generated workspace contains the
  exact Factory candidate manifest plus Captain-supplied Hermes skill receipt.
- [x] Remove the unconditional n8n shape from `FactoryCandidateManifest`.
  Non-integration candidates such as Claims must allow empty workflow, schema,
  and n8n-tool tuples; n8n candidates must retain exact workflow plus input and
  output schema consistency. Dummy `unused_manifest_tool` artifacts are not
  acceptable promotion evidence.
- [x] Pin Hermes execution to the reviewed worktree/submodule and released skill
  directories. `HermesCliSettings.module_root` executes the reviewed module
  through the selected Python interpreter with an exact module root, while the
  external Factory skill directory points at this worktree. The deployable
  operator passes the exact module root, provider, model, usage-evidence path,
  and a 0.10 USD aggregate Hermes ceiling explicitly.
- [x] Validate the Forge `skill_usage_receipt_ref` against the exact released
  skill, Factory job, attempt, and lease before accepting `AGENT_CODE_CREATED`;
  schema-only JSON is not sufficient evidence that Hermes used the skill. The
  receipt is now derived only from the exact Hermes inventory and Codex brief,
  passed separately into Forge, retained byte-for-byte in CAS, and revalidated
  by Gateway.
- [x] Separate the private technical holdout from the 15-case business suite.
  Captain exposes only one redacted mandatory-escalation task to the generated
  team, retains the expected decision/rationale/handoff privately, and binds
  evaluation to the exact Gateway candidate and Captain-scoped tool subset.
- [x] Provision immutable v4 demo jobs in isolated `captain_test` with a strict
  total configured ceiling of 1.00 USD: 0.10 USD for Hermes and 0.45 USD per
  benchmark team. Claims job `98a8c3d5-b6a5-5c59-8c0f-811280f319e4` and Renewal
  job `7eaa2fad-2b1f-5eed-a902-9af7fc9f60c5` currently stop at the redacted
  `factory_dispatch_required` checkpoint. No provider call was made.
- [ ] Provision immutable v5 demo jobs after the Captain Codex seal release.
  V5 replaces, rather than mutates, the v4 jobs and includes the seventh
  `seal_codex_build` release plus exact Codex-to-Minibook source provenance.
- [x] Prove that the generated source archive is the exact implementation
  produced from Hermes' Captain-approved Codex brief. The current receipt binds
  the brief, isolated Codex worktree, redacted CLI session evidence, candidate
  manifest, test evidence, and exact source ZIP in a Captain-issued receipt.
  Minibook V2 imports those already-sealed source bytes unchanged and preserves
  the Captain receipt as a separate package edge.
- [ ] Run the two provider-backed Factory jobs and 30-case team/baseline
  benchmark after `OPENAI_API_KEY` is securely exported into the invoking
  process. Retain cost, latency, tool, handoff, recovery, Gateway decision, and
  Minibook projection evidence; do not call either team `ready_to_use` before
  those gates pass.

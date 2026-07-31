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
- [x] Provision immutable v5 demo jobs after the Captain Codex seal release.
  V5 replaces, rather than mutates, the v4 jobs and includes the seventh
  `seal_codex_build` release plus exact Codex-to-Minibook source provenance.
  Claims job `2113392c-c5f0-5bb1-95ee-48c4c31bc14d` and Renewal job
  `6c156f2e-dd52-54bd-97f5-a3fe788815f9` stop at the expected
  `factory_dispatch_required` checkpoint. No provider call was made.
- [x] Prove that the generated source archive is the exact implementation
  produced from Hermes' Captain-approved Codex brief. The current receipt binds
  the brief, isolated Codex worktree, redacted CLI session evidence, candidate
  manifest, test evidence, and exact source ZIP in a Captain-issued receipt.
  Minibook V2 imports those already-sealed source bytes unchanged and preserves
  the Captain receipt as a separate package edge.
- [x] Replace seeded discovery's large model-authored inventory echo with a
  small digest-bound Hermes attestation. Captain now materializes the exact
  content-addressed inventory only after validating that attestation, and the
  Hermes one-shot runtime preserves an explicit empty tool scope instead of
  falling back to configured tools.
- [x] Persist Attempt-1 discovery in the durable Factory replay store and load
  that exact typed inventory across the later Tool Integrator lease. Captain now
  builds the canonical V3 `CodexBuildBriefV1` and prompt in its private CAS;
  tool-free Hermes returns only
  `hermes.factory-codex-brief-attestation.v1` bound to the brief digest. Retry
  attempts reuse the same inventory and add only Captain-authorized improvement
  evidence. Full model-authored brief reproduction is no longer on the V3 path.
- [x] Retain streamed Codex terminal evidence and a typed, redacted recovery
  checkpoint for timeout/cancellation. The local recovery path preserves the
  terminal exit cause, checkpoint/receipt references, and immutable Captain
  resume bindings without exposing workspace paths, journals, prompts, or
  stderr. This is local implementation evidence only, not a provider run.
- [ ] Run the prepared immutable v19 two-profile Factory scope and 30-case
  team/baseline benchmark after `OPENAI_API_KEY` is securely exported into the invoking
  process. Retain cost, latency, tool, handoff, recovery, Gateway decision, and
  Minibook projection evidence; do not call either team `ready_to_use` before
  those gates pass. The interactive
  `scripts/run-business-benchmark-demo-secure.ps1` wrapper accepts the key via
  a masked prompt, keeps it process-only, and clears it in `finally`.
  The v11 run failed closed during seeded discovery after Hermes changed typed
  bindings; v12 proved the new discovery attestation and then failed closed at
  `brief_codex`. The authorized v13 and v14 retries also failed closed there:
  v13 exposed missing Captain job context, while v14 exposed a narrowly
  malformed Hermes nesting/Invocation echo. Those failures triggered the
  architectural replacement above instead of further prompt tuning. Brief skill
  release v2 and immutable suite/seed v15 were executed with a `0.09` USD Hermes
  ceiling and reached the Codex build boundary, where the WindowsApps native
  binary failed ProcessStart with access denied. The next resumable execution
  probes a launchable native npm Codex binary. Because each failed replay is
  immutable, v16 retained the expected dirty-base rejection while the fix was
  still uncommitted. The clean v17 run reached a real generated Claims candidate
  but timed out at 900 seconds because Codex started the repository-wide test
  suite instead of candidate-scoped verification. Fresh suite/seed v18 carries
  explicit generated-candidate compile/pytest commands, defers `pytest.live.demo`
  to Captain's sealed holdout, and uses a reduced `0.06` USD Hermes ceiling so
  cumulative worst-case spend remains below `1.00` USD. V18 still exhausted the
  900-second Codex lease before producing the three required output artifacts.
  `PowerShellCodexRunner` returned exit code 124 with empty buffered JSONL, which
  `_session_receipt` previously masked it as `Codex JSONL evidence is empty`. Further
  paid retries were stopped until Codex output was streamed durably, timeout
  evidence retained its real terminal cause, and the build was split into
  resumable Captain-authored scaffold/test and Codex implementation phases.
  Those local recovery changes are now complete. Suite/seed v19 was then
  provisioned and provider-executed for candidate construction only. Claims
  produced the exact inventory, Codex brief, and sealed build artifacts before
  the first run failed closed at the Forge evidence mapping boundary. A later
  resume proved that the shared disposable `captain_test` ledger had been
  cleared while the private Hermes replay remained, so Captain rejected the
  fresh-lease/replay mismatch. No business benchmark, Gateway promotion, or
  Minibook projection ran. Actual provider cost was not materialized as a
  readable usage receipt; configured ceilings remained `0.06` USD aggregate
  for Hermes and `0.30` USD per team.
- [x] Isolate provider-live benchmark state from ordinary MariaDB test cleanup.
  `benchmark-start` now creates a dedicated `captain-cook-business-benchmark`
  Compose project on its own port, uses a named persistent MariaDB volume, and
  writes only an ignored runtime DSN contract for the benchmark runner. The
  ordinary `captain_test` container remains disposable and cannot clear the
  benchmark ledger. A controlled dedicated-container restart retained all 13
  initialized schema tables. Gateway reuse and termination additionally require
  the exact PID, start time, executable, listener PID, and a SHA-256 binding of
  the expected endpoint plus ledger DSN; a merely healthy foreign Gateway fails
  closed. This is infrastructure evidence only; it does not repair v19's
  missing Captain blocks or authorize a benchmark rerun.
- [x] Enforce the operator's `1.00 EUR` marginal-cost ceiling per team before
  provider construction. The live demo reserves at most `0.30 USD` for one
  team's candidate/baseline benchmark and conservatively assigns the complete
  two-team Hermes ceiling of `0.06 USD` to either team. Codex is admitted only
  when `codex login status` proves ChatGPT subscription authentication, so its
  metered API reserve is exactly zero. Captain rejects any composition whose
  worst-case `0.36 USD` per team exceeds the internal `0.40 USD` ceiling, any
  API-key-authenticated Codex session, or any missing/non-canonical cost field.
  The internal ceiling is intentionally far below the user ceiling rather than
  depending on a live exchange-rate lookup during dispatch. This guard was
  verified without a provider call; it does not claim that v19 has resumed.

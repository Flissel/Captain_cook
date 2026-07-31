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
- [x] Add a Captain-owned runtime-retry authorization writer. Captain now
  verifies the immutable interrupted checkpoint plus terminal receipt, issues
  one time-bounded exact resume authority, stores it write-once below the
  private runtime namespace, and threads the matching authority into the live
  composition. The PowerShell runner persists the redacted interruption
  checkpoint before exiting `2`; a separate explicit issuer consumes it. The
  build cannot self-authorize. Improvement authorization after a completed
  failed evaluation remains a distinct pending contract and cannot substitute
  the pre-provisioned seed candidate for generated Forge code.
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
- [ ] Run the prepared immutable v21 two-profile Factory scope and 30-case
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
  for Hermes and `0.30` USD per team. V20 then ran Claims candidate
  construction against the dedicated ledger. Codex completed, retained its
  streamed terminal evidence, and produced the manifest, source archive, and
  test evidence. Captain still failed closed while sealing because the
  pre-seal manifest contained `source_archive_ref`, whose archive digest is
  necessarily self-referential. Renewal construction, the 30 business cases,
  Gateway promotion, and Minibook projection did not run. The Codex prompt now
  requires the pre-seal manifest to omit that field; Captain remains the only
  component allowed to add it after sealing. The failed V20 seal replay is
  immutable, so suite/seed V21 is the next authorized fresh scope rather than
  a relabelled or edited V20 artifact.
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
- [x] Retire v19 from further execution without rewriting its private evidence.
  The dedicated persistent ledger has no v19 jobs, while the private Claims
  replay remains bound to the lost v19 lease through discovery, Codex brief,
  and sealed-build evidence. Re-provisioning v19 would therefore create a new
  lease that correctly conflicts with the old immutable replay. Suite/seed v20
  created fresh Claims job `f5263d6f-5d91-5794-8a5d-8b8aa8b83643` and Renewal
  job `9b710b88-41dd-59c9-a65b-063e29d422b0` under the same case, model, and
  cost contracts. V20 Claims provider execution reached Codex completion but
  failed at the self-referential pre-seal manifest boundary described above.
  V21 therefore provisions new immutable jobs after the prompt correction.
  Its no-provider plan binds Claims job
  `c36882f3-792a-528c-9c99-6dcbf2dfc9cb` and Renewal job
  `63cc1810-fd20-50cb-a110-d96d016066c2` to suite 21 and seed
  `business-benchmark-demo-2026-07-v21`; plan evidence explicitly records no
  provider, live-service, provisioning, Gateway, or Minibook mutation. The
  first V21 Claims execution then completed Codex, produced all three output
  artifacts, and reached Captain's sealed checkpoint, proving the pre-seal
  manifest correction. It next failed closed before launching Minibook Forge:
  the composition routed the new `captain-codex-build` source reference to the
  benchmark-only materializer. Renewal, technical holdout execution, all 30
  business cases, Gateway promotion, and Minibook projection did not run. The
  local composition now routes benchmark and Codex-build references to their
  exact owning CAS and verifies content digests before materialization. The
  sealed V21 Claims replay remains immutable and is eligible for reuse only
  through the same Captain lease/job binding. A 14-second resume then reused
  that seal and let Minibook create the V2 projection package without another
  Codex run, but Gateway failed closed because its candidate provider parsed
  only the legacy V1 package contract. The shared boundary now accepts V2 only
  when its Captain Codex receipt is present in the agent-code artifact set,
  canonical-digest verified, bound to the exact job/attempt/source, and bound
  to the unique `factory-candidate.json` inside the sealed source archive.
  Legacy V1 remains supported. This is locally verified adapter evidence;
  the next replay reached the external candidate and failed on the canonical
  JSON `schema` field because `FactoryCandidateManifest` previously accepted
  only its internal `schema_name`. The contract now reads both spellings for
  backward compatibility and serializes only canonical `schema`; the actual
  V21 Claims candidate parses under that corrected contract. Its technical
  holdout then rejected the candidate: the archive contains prompts, a static
  manifest, and manifest-only tests but no executable AutoGen team, while its
  declared real-case command runs pytest rather than a real team and cannot
  emit Captain's terminal result contract. It is not ready to promote and must
  receive a separately authorized improvement attempt. Renewal produced an
  executable team scaffold but timed out after 900 seconds before its three
  final artifacts. Captain retained exit `124`, the streamed session evidence,
  and exact resume ordinal `1`; no new suite or Hermes call is required for the
  one authorized resume. All 30 business cases, promotion, and projection
  remain unproven.
  Renewal Resume 1 subsequently completed in the original Codex thread,
  retained streamed JSONL, produced the required artifacts, and reached the
  sealed Captain checkpoint without a new Hermes call. Gateway then accepted
  the recovery-bound tool-candidate block only after exact retry-reference,
  lease, job, attempt, and evidence-time validation. The technical holdout
  still failed `business_value` and `mandatory_handoff` while `safe_tool_use`
  passed. Therefore both Claims and Renewal require explicit Captain
  improvement authorization; neither candidate may enter the 30-case gate yet.
- [x] Enforce the operator's `1.00 EUR` marginal-cost ceiling per team before
  provider construction. The live demo reserves at most `0.30 USD` for one
  team's candidate/baseline benchmark and conservatively assigns the complete
  two-team Hermes ceiling of `0.06 USD` to either team. Codex is admitted only
  when `codex login status` proves ChatGPT subscription authentication, so its
  metered API reserve is exactly zero. The prior V20 and first V21 attempts are
  each additionally reserved at `0.06 USD` per team because their actual
  Hermes usage was not materialized durably. Captain rejects any composition
  whose cumulative worst-case `0.48 USD` per
  team exceeds the internal `0.50 USD` ceiling, any
  API-key-authenticated Codex session, or any missing/non-canonical cost field.
  The internal ceiling is intentionally far below the user ceiling rather than
  depending on a live exchange-rate lookup during dispatch. This guard was
  verified without another provider call. Both prior configured reserves are
  counted conservatively; this does not claim that V19 or V20 can resume or
  that V21 has passed its technical holdout.

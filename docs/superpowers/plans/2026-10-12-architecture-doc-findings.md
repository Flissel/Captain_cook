# Architecture findings from the 2026-10-12 documentation pass

Source: [`docs/architecture/`](../../architecture/00-overview.md), section 5 of
the overview. Each item was checked against code (file:line evidence lives in
the part documents). Nothing here has been fixed yet.

## Correctness (fix first)
- [x] Stranded batch (`7d63dfb`, 2026-10-13): `gateway/contracts.py::project_batch` never resets
  `recovery_recorded` on `batch_claimed`; a second expired claim stays
  `claimed` forever. Reset on claim or compare
  `recovered_iteration == claim_iteration`; add a test that expires the second
  claim (extend `tests/gateway/test_gateway.py:1217`).
- [x] Heartbeat shortens lease (`08544d6`, 2026-10-13; renews to claim_ttl): `gateway/store.py:6694` sets now + 30 min while
  claim TTL is 90 min; never move expiry earlier.
- [x] Release before review (`e73fdc4`, 2026-10-13; plan published first, review gate holds default releases when approval is on): `AutonomousCaptainPlanner.run` releases batches as
  `pending` before plan publish/review; release as `pending_review` (or publish
  and review first) when a gateway is configured.
- [ ] Regenerate `gateway/capability_factory_runtime_closure.json` and the
  factory runtime descriptor (`scripts/generate-capability-factory-closure.py`,
  `scripts/generate-capability-factory-descriptor.py`): the closure dates from
  2026-08-17 and no longer matches most of its 174 modules after M0/M1a and the
  d11 merge, so production capability attestation would fail at startup.
- [ ] Escalation dead end: recorder has no `EscalateToRedecompose` handler, so
  exhausted subproblems stay in RETRYING; record a terminal outcome when
  re-decomposition yields no children.

## Wiring gaps
- [ ] Call `recover_on_startup` and `verify_chain` outside tests (startup/CLI).
- [ ] Emit `hermes_tool_gap_recorded` and the missing Minibook projection types
  (feed emits 3 of 10).
- [ ] Wire `agenten/coding_agent/` into the factory (M1b/G1 already plan this).
- [x] Capability demo operator: provide `gateway.capability_assembly_bootstrap`
  and remove the unconditional `DemoConfigurationError` in `start`/`status`
  (landed with the d11 merge `e198a09`, 2026-10-13; offline tests only — the
  paid live demo has not been run).
- [ ] Live-run the capability demo operator (`start`/`status`/`run`/`resume`)
  once a live gate is authorized.

## Integration backlog (2026-10-13)
- [ ] Factory v3 (`codex/captain-hermes-minibook-factory-v3`, WIP merge on
  local branch `integrate/d11-factory-v3` at `9ceeb98`) is parked: v3's
  hermetic semantic verifier (`agenten/agent_factory/hermetic_build_authority.py`)
  proves only its eight sales-pipeline assertion paths, while the d11 compiler
  emits 22 generic assertions (`*_integration/_failure/_tool`, `authority_N`,
  `security_N`, `stop_N`, agent real cases). Decide: generic verifier (preferred)
  or two assertion profiles; then finish the merge (4 v3 tests + 1 settings fake).
- [ ] Triage remaining Codex branches from 2026-07-21 (`package-c`,
  `capability-live-adapters`) and draft PR 23 — likely superseded.
- [x] Pin LF for d11 runtime authority files (`df8665b`); Windows CRLF checkouts
  failed digest attestation.
- [ ] Upgrade Hermes to upstream v0.21.7: the 20 Captain fork commits are
  ported on branch `captain/v0.21.7-port` of Flissel/hermes-agent (`c8d8946`;
  GPT-4.1 workaround dropped, upstream covers it). Blocker: v0.21.7 only
  supports Python 3.14 (all core deps gated on `python_version >= '3.14'`),
  while Captain (3.11) launches Hermes with its own interpreter
  (`python -m hermes_cli.main`). Needs a separate 3.14 Hermes environment,
  launcher/setup/CI changes, then the ported tests and a live `hermes -z` smoke
  before the submodule pin moves.
- [ ] After the hackathon: evaluate CortexDB (liliang-cn/cortexdb) as a scoped,
  read-only agent memory / ledger projection. Holdouts must never be recallable
  in generation context; never a second source of truth.
- [ ] Tests that spawn `<repo>/.venv/Scripts/python.exe` fail in worktrees
  without a local `.venv` (e.g. `test_generate_runtime_adapter_manifest.py`);
  consider `sys.executable` or a documented worktree venv.

## Hardening
- [ ] Gateway `POST /blocks`: reject `agent_factory_block` under a `work_batch`
  parent (block type vs parent type check).
- [ ] `MariaDBStorage.save/clear` issue `DELETE FROM blocks`; forbid or remove
  for the gateway ledger.

## Contract and doc drift
- [ ] Unify terminal vocabularies (`batch_done` outcomes vs
  `DeliveryBatchDonePayload`, factory terminal states, release decisions).
- [ ] Repair ceiling: align the 07-17 gateway-native delivery spec (3) with code (5).
- [ ] `TO_BE_BUILT.md`: align spec headings, `input_document.py` required
  sections, the root file and the runbook (`input.md`).
- [ ] AGENTS.md: Captain also owns an n8n stack (5679, `owned-n8n` profile on
  5678); unroutable work is now a terminal FAILED stage.
- [ ] Port defaults: Mailpit 8025 vs 18025, Minibook 8080 vs 3456.
- [ ] `docs/ARCHITECTURE.md`: stale `LedgerStorage` advice (line 353), "sole
  writer" wording (line 423), missing event-driven path; link
  `docs/architecture/`.

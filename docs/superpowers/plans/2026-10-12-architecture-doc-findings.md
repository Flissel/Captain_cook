# Architecture findings from the 2026-10-12 documentation pass

Source: [`docs/architecture/`](../../architecture/00-overview.md), section 5 of
the overview. Each item was checked against code (file:line evidence lives in
the part documents). Nothing here has been fixed yet.

## Correctness (fix first)
- [ ] Stranded batch: `gateway/contracts.py::project_batch` never resets
  `recovery_recorded` on `batch_claimed`; a second expired claim stays
  `claimed` forever. Reset on claim or compare
  `recovered_iteration == claim_iteration`; add a test that expires the second
  claim (extend `tests/gateway/test_gateway.py:1217`).
- [ ] Heartbeat shortens lease: `gateway/store.py:6694` sets now + 30 min while
  claim TTL is 90 min; never move expiry earlier.
- [ ] Release before review: `AutonomousCaptainPlanner.run` releases batches as
  `pending` before plan publish/review; release as `pending_review` (or publish
  and review first) when a gateway is configured.
- [ ] Escalation dead end: recorder has no `EscalateToRedecompose` handler, so
  exhausted subproblems stay in RETRYING; record a terminal outcome when
  re-decomposition yields no children.

## Wiring gaps
- [ ] Call `recover_on_startup` and `verify_chain` outside tests (startup/CLI).
- [ ] Emit `hermes_tool_gap_recorded` and the missing Minibook projection types
  (feed emits 3 of 10).
- [ ] Wire `agenten/coding_agent/` into the factory (M1b/G1 already plan this).
- [ ] Capability demo operator: provide `gateway.capability_assembly_bootstrap`
  and remove the unconditional `DemoConfigurationError` in `start`/`status`.

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

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
- [ ] Compose the full Factory resume entry point from real ports. The missing
  production pieces are a `CreationJobMapper`, a content-addressed
  Forge-result-to-`ResolvedFactoryCandidate` binding, live
  `TeamExecutionService` plus business-benchmark dispatch wiring, and a
  Captain-owned retry/improvement authorization writer. Until all four exist,
  the bounded runner must stop at its typed checkpoint and must not substitute
  the pre-provisioned seed candidate for newly generated Forge code.

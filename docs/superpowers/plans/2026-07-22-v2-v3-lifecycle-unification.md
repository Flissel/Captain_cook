# V2/V3 Capability-Release Lifecycle Unification

**Status:** required before provider-live Package-C completion

## Observed failure

The Package-C entrypoint persists the canonical creation lifecycle as an
`AgentFactoryJobV2`.  Its V3 evidence bridge then creates a distinct
`AgentFactoryJobV3` and persists all role leases for that second job before it
has advanced through the Gateway action sequence.  The Gateway correctly
rejects the second lease with:

`factory lease role is not the next authorized action`

Even if that validation were weakened, a V3 lease cannot validate a V2 Captain
block because their job identities differ.  Relaxing this check would create
unverifiable evidence and is not an acceptable live-demo shortcut.

## Gaps to close

- [ ] Define one Captain-owned release lifecycle identity shared by creation,
  candidate validation, provider execution, quality feedback, promotion, and
  projection.
- [ ] Replace the shadow V3 lease issuance in
  `capability_v3_evidence_bridge.py` with a coordinator-owned, just-in-time
  lease source.
- [ ] Execute the six skill phases through `FactorySixSkillLiveCoordinator`,
  binding the Minibook-created sealed candidate to the `SUBMIT_FORGE_JOB`,
  build-validation, real-case, and quality phases without regenerating it.
- [ ] Make controlled recovery an explicit `DISPATCH_REAL_CASE_TESTER` effect
  in that same lifecycle, followed by three distinct normal provider runs.
- [ ] Derive the release receipt and Package-C terminal decision from the
  Gateway-persisted workflow artifacts and leases, not from a parallel V3
  bridge ledger.
- [ ] Add a MariaDB Gateway integration test that rejects cross-job leases and
  proves one successful recovery-plus-three-run lifecycle using one job ID.
- [ ] Run the provider-live gate only after the integration test is green;
  then verify Gateway promotion, execution, and the Minibook projection for
  the same correlation ID.

## Acceptance evidence

The completed implementation must show, for one correlation ID:

1. each persisted lease is recorded only when its role is the Gateway's next
   action;
2. all six skill artifacts, candidate attestation, recovery record, and three
   normal provider traces bind the same Captain job;
3. the Gateway reaches `ready_to_use` before publication;
4. the runtime execution and the Minibook projection read back the same
   correlation ID; and
5. no synthetic evidence, bypassed lease validation, or VibeMind n8n mutation
   is used.

# Task 3B: durable benchmark claims and replay

## Scope delivered

- Added frozen effect-identity, runtime-preparation, prepared-effect, claim,
  provider-fence receipt, recovery-observation, snapshot, and acquisition
  contracts.
- Added in-memory and append-only filesystem replay stores with canonical JSON,
  atomic no-replace writes, per-effect locking, separately derived claim
  fingerprints, monotonically increasing fences, and exact terminal receipt
  bytes.
- Extended the Task 3A executor port with `prepare`, provider-side
  `register_fence`, fenced `execute`, and proof-bound `recover` boundaries. The
  paired coordinator now requires an explicitly injected replay store.
- Bound durable effect identity to job, correlation, subject version, attempt,
  suite reference and digest, suite/case IDs, variant, full execution-policy
  digest, and candidate/baseline variant-policy digest. Task 3A request identity
  now also includes subject version.
- Preserved candidate/baseline independence: each variant has its own effect
  identity, claim fingerprint, claim ID, and fence sequence.
- Added restart behavior that replays an exact terminal receipt without executor
  calls, blocks active pending claims, takes a higher fence after expiry,
  registers that fence at the effect provider and persists its content-addressed
  acknowledgement before recovery, persists recovered terminal evidence without
  re-execution, permits execution only after audited `no_effect`, and fails
  closed on `uncertain`.
- Runtime/session addresses now contain the full request identity digest, whose
  binding includes `suite_id`; the previous 16-hex truncation is removed.
- Kept records private and redacted. No case body, provider prose, credential,
  endpoint, local path, provider, database, n8n, Gateway, Task 4, or live adapter
  was added or invoked.

## TDD evidence

1. Initial RED:
   `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_business_benchmark_replay.py`
   failed during collection with the expected
   `ModuleNotFoundError: No module named 'agenten.agent_factory.business_benchmark_replay'`.
2. First GREEN: replay plus authoritative Task 3A execution tests passed:
   `18 passed in 0.81s`.
3. Redaction RED: the focused secret-like prepared-runtime test failed as
   expected because the in-memory store initially accepted `sk-abcdefgh`:
   `1 failed, 8 deselected in 0.68s`. Canonical redaction validation was then
   applied to both stores.
4. Canonical reconstruction RED: a valid but noncanonical pretty-printed
   terminal receipt was initially accepted after filesystem reconstruction:
   `1 failed, 9 deselected in 0.66s`. Reconstruction now rejects noncanonical
   receipt bytes fail-closed.
5. Separate-token RED: the claim-separation assertion initially failed with
   `AttributeError` because claims did not yet expose a variant/fence-bound
   fingerprint: `1 failed, 9 deselected in 0.57s`. Each claim now carries a
   digest binding effect ID, claim ID, and fence.
6. Durable-injection RED: the coordinator initially allowed an omitted replay
   store: `1 failed, 10 deselected in 0.50s`. It now requires explicit replay
   storage rather than silently selecting process-local state.
7. Initial final focused GREEN:
   `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_business_benchmark_replay.py tests/agent_factory/test_business_benchmark_execution.py`
   passed: `21 passed in 0.62s`.

## Review-correction TDD evidence

1. RED: the expanded focused suites failed during collection with two expected
   import errors because `BusinessBenchmarkFenceReceiptV1` did not yet exist.
2. GREEN: `register_fence` now returns a frozen receipt binding the complete
   effect digest, full runtime identity, claim ID, exact fence, registration
   time, and content-addressed evidence. The coordinator persists this receipt
   before either `recover` or `execute`.
3. The deterministic overlap test pauses the fence-1 candidate executor,
   registers fence 2, verifies proof-backed `no_effect`, resumes the old
   executor, observes provider-side stale rejection before effect, and records
   exactly one candidate effect at fence 2.
4. Recovery observations now bind the current claim and exact fence receipt,
   include `checked_at` plus required content-addressed evidence, and reject a
   proofless `no_effect` at schema validation. A follow-up RED failed with
   `1 failed, 14 deselected in 0.69s` because the verified recovery observation
   was not yet reconstructable; the store now persists its canonical bytes per
   effect/fence before the coordinator acts on the result.
5. Full-identity coverage proves different `suite_id` values produce different
   request IDs and runtime addresses containing the complete 64-hex digest.
6. A deterministic two-thread filesystem contention test proves one claim
   acquisition and one active-claim block.
7. Final focused GREEN:
   `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_business_benchmark_replay.py tests/agent_factory/test_business_benchmark_execution.py`
   passed: `25 passed in 0.74s`.

## Final verification

- Full Agent Factory regression:
  `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory`
  passed: `569 passed in 13.08s`.
- Compile gate:
  `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config`
  passed with no output.
- Diff gate: `git diff --check` passed. Git emitted only existing
  LF-to-CRLF advisory warnings for the two modified tracked files.

## Concerns

None. The filesystem store is a private deterministic adapter for this task;
production provider, database, Gateway, n8n, Task 4 scoring, and live adapter
composition remain intentionally out of scope.

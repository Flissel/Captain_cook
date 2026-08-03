# Business benchmark evaluation closeout (2026-08-02)

## Decision

Neither candidate is promoted from the immutable v34 benchmark. Both final
evaluations remain `failed` because the v34 policy enforces relative cost and
latency ceilings of 1.25x and 1.50x. The result is deliberately not rewritten
after observing the holdout.

The run used `gpt-4.1-mini` for the paired business cases and the Captain-owned
Hermes/Codex improvement route configured for `gpt-5.6-terra` with high
reasoning. All provider work stayed in the isolated `captain_test` scope.

## Immutable final evidence

| Profile | Attempt | Candidate vs baseline quality | Candidate-only safety | Relative efficiency | Gateway summary |
| --- | ---: | --- | --- | --- | --- |
| Claims | 3 | correctness 100% vs 100%; rationale 100% vs 100%; completion 100% vs 80% | 0 unsafe tools; 0 missed required Captain reviews | cost 4.0859x; latency 3.8226x | `85d892e1822fcfc8650a9eae8c0e26d77b558060706c005415ca6fb81297e977` |
| Renewal | 5 | correctness 100% vs 93.34%; rationale 100% vs 93.34%; completion 100% vs 60% | 0 unsafe tools; 0 missed required Captain reviews | cost 2.6239x; latency 3.2746x | `ae36a91f32e95fd2eb9082ba03d6c7cc9776eddf10a4e9ae04e9f826bb719a6e` |

Claims consumed USD 0.065272 of its USD 0.32 Gateway benchmark budget and
Renewal consumed USD 0.093224. These figures do not claim total provider cost
outside the Gateway benchmark ledger.

The v34 summaries report three and six `mandatory_handoff_missed` events. The
redacted case metrics prove that these are baseline-only misses: every required
candidate review was completed through the explicit Captain operator path.
The v34 summary contract intentionally remains unchanged and audit-valid.

## Pipeline corrections completed

- The reserved `captain_business_decision` host call no longer creates a false
  `none` integration intent when a real n8n tool also ran.
- The demo runner now gives the explicit Captain operator 120 seconds to
  complete a required review instead of returning immediately with an accepted
  but incomplete handoff.
- Improvement briefs now permit bounded system-prompt tuning, identify
  Captain-owned human review as external evidence, and instruct Codex to reduce
  model turns, prompt tokens, and serial handoffs for efficiency failures.
- A backward-compatible, opt-in `candidate_only_safety_gates` policy mode is
  available. Legacy policies omit the field and retain their original digest
  and semantics.

## GAPS_TO_CLOSE for a fresh v36 benchmark

The v35 Claims build remains immutable technical-failure evidence: inherited
Codex plugins blocked the first process and its two bounded resumes never
produced candidate artifacts. v36 keeps the same predeclared business-value
policy but derives fresh job IDs and fresh Codex threads after plugin isolation.

- [x] Define the business-value gate before creating new holdouts. Require a
  material correctness or completion uplift over the baseline, zero candidate
  safety violations, and zero candidate missed Captain reviews. The v35 policy
  requires at least 500 basis points correctness uplift or 1000 basis points
  completion uplift.
- [x] Decide whether relative cost/latency ratios remain hard stops or become
  reported diagnostics behind the existing absolute per-case cost and latency
  ceilings. v35 uses diagnostics; absolute ceilings remain hard stops.
- [x] Opt the v35 policy binding into `candidate_only_safety_gates=true` while
  retaining baseline safety and handoff flags as diagnostic case metrics.
- [x] Bump the seed and suite version to v35 and derive new dry-run job IDs;
  never mutate or reinterpret the v34 summaries. Applying those jobs remains
  part of the paid run.
- [ ] Run a fresh 15+15 paired benchmark for each team from a clean checkout.
- [ ] Promote through Captain/Gateway only when the predeclared v35 policy on
  the fresh v36 suite is
  green; otherwise retain the candidate as evaluated but not ready-to-use.

## Non-claims

- Thirty paired receipts prove evaluation coverage, not production deployment.
- A completed Captain review proves the handoff occurred; it does not prove a
  real insurance or commercial decision was approved.
- No v34 candidate was promoted, and no production Minibook or VibeMind service
  was mutated by this evaluation.

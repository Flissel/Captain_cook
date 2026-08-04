# Business benchmark evaluation closeout (updated 2026-08-04)

## Decision

Neither v40 candidate is promoted. The fresh cloud evaluation completed all 30
paired cases with no missing receipts and zero unsafe candidate tool uses, but
both predeclared business-value gates remain `failed`.

- Renewal is technically and professionally strong: 15/15 correct decisions
  versus 14/15 for the baseline, with complete rationale on every case. It is
  not ready-to-use because all six required Captain human-review receipts
  remained incomplete under the bounded one-second demo review window.
- Claims produced 14/15 correct decisions versus 15/15 for the baseline. One
  mandatory-escalation case exceeded the sealed tool-call ceiling. The runtime
  recorded that paid effect as `policy_failed` with resolved usage evidence and
  continued the suite instead of aborting it. All three mandatory Captain
  reviews remained incomplete.

The post-Factory Gateway preflight therefore remains
`factory_dispatch_required`; no promotion, release execution, or production
Minibook projection is claimed.

The run used `gpt-4.1-mini` for the paired business cases and the Captain-owned
Hermes/Codex improvement route configured for `gpt-5.6-terra` with high
reasoning. All provider work stayed in the isolated `captain_test` scope.

## Fresh v40 evidence

| Profile | Candidate vs baseline quality | Candidate-only safety | Efficiency diagnostics | Disposition |
| --- | --- | --- | --- | --- |
| Claims | correctness 93.34% vs 100%; rationale 93.34% vs 100%; completion 80% vs 80% | 0 unsafe tools; 3 missed required Captain reviews; 1 `max_tool_calls` policy failure | cost USD 0.016380 vs 0.003948 (4.149x); latency 116632 ms vs 38038 ms (3.0662x) | `failed` |
| Renewal | correctness 100% vs 93.34%; rationale 100% vs 93.34%; completion 60% vs 60% | 0 unsafe tools; 6 missed required Captain reviews | cost USD 0.020372 vs 0.007670 (2.6561x); latency 137836 ms vs 46551 ms (2.9610x) | `failed` |

Canonical identities and immutable artifacts:

- Claims job `b603776f-dc07-5381-8c79-673d5ee610fc`, suite
  `claims-business-benchmark-v40-10e89971c818`, candidate ZIP
  `182dd8e9b0efc595b336beae7ab31d053256d08a092d81792e13187088ed1beb`,
  summary artifact
  `81194398f3c8665c13657dfc525cb86068216f75c3fc774726c35edf7b6c3792`.
- Renewal job `ee563189-b7f3-5066-b2cf-c23a02f3e30f`, suite
  `renewal-business-benchmark-v40-4ff62362828b`, candidate ZIP
  `44120dff30d288f2a6963de91165beabd16f27d2d0e31ad4c1bc44d5b2ee609c`,
  summary artifact
  `aaa73dc9fdc0802a898b721e9ff5cac52c626118cff0538b61e766c184875f14`.

The Claims interruption artifact records `reason_code=max_tool_calls`,
`provider_started=true`, and `usage_resolved=true`. This is candidate-quality
evidence, not an infrastructure failure.

## Historical immutable v34 evidence

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
- AutoGen message, handoff, and tool-call ceiling violations now become
  evidence-bound `policy_failed` provider receipts. A bad case no longer
  destroys the remaining benchmark coverage.
- The Captain review boundary now has an opt-in delegated-operator adapter.
  It runs outside the provider port, is restricted to exact immutable job IDs
  and an exact completion count, and records only redacted acknowledgement
  evidence. Without an explicit operator ID, review completion remains closed.
- The Claims seed now permits exactly two business-decision calls, makes the
  ordinary and escalation routes mutually exclusive in both specialist
  prompts, and explicitly forbids a third call.
- Hermes paid-effect accounting is scoped to the benchmark suite while retry
  authority remains in its canonical sibling store.
- The Renewal n8n integration uses the active official native-node workflow;
  its valid, reject, and deployment-smoke executions were verified separately.

## GAPS_TO_CLOSE after v40

The v35 Claims build remains immutable technical-failure evidence: inherited
Codex plugins blocked the first process and its two bounded resumes never
produced candidate artifacts. v37 keeps the same predeclared business-value
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
- [x] Run a fresh 15+15 paired benchmark for each team with a clean tracked
  worktree and isolated `captain_test` services.
- [x] Apply the predeclared v35 policy to the fresh v40 suite and retain both
  failed candidates as evaluated but not ready-to-use.
- [x] Implement a real Captain human-review completion adapter for the six
  Renewal and three Claims escalation cases. The v41 dry-run was effect-free;
  its first Factory build then failed before Codex launch because the base was
  dirty, so v41 remains immutable technical-failure evidence.
- [x] Tighten the Claims escalation prompts/tool routing so no valid mandatory
  escalation can request a third business-decision call.
- [x] Run immutable v42 from its clean committed base. Both 15-case paired
  suites completed with zero missing receipts and zero unsafe tool uses, but
  Captain correctly rejected both candidates. Claims scored 8000 vs 10000
  correctness bps with three mandatory handoff misses; Renewal scored 5334 vs
  9334 with six mandatory handoff misses. The summaries are retained under
  IDs `4c0774fd-8bec-5eee-858c-624ace3191ca` and
  `a44662b5-4063-5e56-b401-0d510340852e`.
- [x] Preserve the first v42 runtime failure and fix its root cause with a
  90-minute suite-only `REAL_CASE_TESTER` lease that remains capped by the
  immutable job deadline; ordinary Factory role leases remain 15 minutes.
- [x] Diagnose the v42 handoff misses: the external Captain completion adapter
  started after the Factory Quality Warden had already run the provider-backed
  benchmark. Move the adapter before Factory dispatch and cover the ordering
  contract with a regression test.
- [x] Run immutable v43 without reinterpreting or overwriting v42. Renewal
  passed at 10000 correctness, completion, and rationale bps versus the
  9334/6000/9334 baseline, with zero handoff misses, unsafe tools, or missing
  receipts (`c361eed6-b692-5b61-af58-eefd0dc6576d`). Claims Attempt 2 improved
  to 9334 correctness/completion/rationale bps versus 10000/8000/10000, with
  zero unsafe tools and zero missing receipts, but Captain correctly rejected
  it for one mandatory handoff miss
  (`24ba428c-fbe3-50cd-acf9-f07e1c634fcd`).
- [x] Align the external completion adapter's own timeout validator with the
  5400-second suite lease. The recovery operator completed all nine exact v43
  review requests (Claims 3, Renewal 6), but the first Claims miss was already
  sealed before that recovery process started. The pre-dispatch watcher fix is
  therefore code-complete but still needs a fresh Claims evaluation to prove
  zero misses.
- [x] Permit only sealed system-prompt digest changes after a Gateway-authorized
  improvement attempt. Attempt 1 remains byte-identical to the normative public
  contract, and all topology, tools, handoffs, memory, conversation, and limit
  fields remain exact for every later attempt.
- [x] Preserve Claims Attempt 3 as immutable failed evaluation evidence. Its
  summary `90ccd274-d3d6-5977-b752-f2519d780c62` scored the candidate
  8000/8000/8000 correctness/completion/rationale bps versus
  10000/8000/10000 for the baseline. All three mandatory escalations missed
  Captain completion because the watcher counted old Attempt-2 receipts; no
  unsafe tool use or missing run receipt occurred.
- [x] Bind the external review watcher to exact `(job_id, attempt)` pairs so
  completed reviews from an older immutable attempt cannot satisfy a newer
  attempt.
- [x] Preserve Claims Attempt 4 as a technical interruption, not a business
  evaluation. Twenty-six provider effects finalized before the third mandatory
  escalation candidate hit an OpenAI upstream reset before response headers;
  the final effect remains durably `dispatching` with unresolved usage and no
  summary. Captain correctly forbids automatic reexecution of that paid effect.
- [x] Prevent the wrapper from dispatching the provider benchmark a second time
  after Factory already dispatched Quality Warden. A completed failed
  evaluation now returns the typed `factory_improvement_required` checkpoint
  directly instead of a misleading post-Factory dispatch checkpoint.
- [ ] Run a fresh Claims-only immutable evaluation with the corrected watcher,
  using the fresh v44 suite/job identities after the unresolved Attempt-4
  effect. Promote only if mandatory handoff misses become zero and correctness
  is not below baseline.
- [ ] Re-run from a clean checkout and promote only if correctness is not below
  baseline, every mandatory handoff is completed, and safety remains perfect.

## Non-claims

- Sixty v40 run receipts (30 candidate plus 30 baseline) prove evaluation
  coverage, not production deployment.
- A completed Captain review proves the handoff occurred; it does not prove a
  real insurance or commercial decision was approved.
- No v34 or v40 candidate was promoted, and no production Minibook or VibeMind
  service was mutated by this evaluation.

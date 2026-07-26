# Business Benchmark Gate Design

## Decision

Captain adds a deterministic business-benchmark gate to the existing Hermes
six-skill creation pipeline. The gate proves whether a generated AutoGen team
is useful for a named business outcome, rather than merely proving that its
build, tool calls, and predeclared observables work.

The first two governed profiles are `insurance_claims_resolution_swarm` and
`customer_renewal_orchestration_team`. Each receives a versioned, anonymized
case set and the exact same cases are run by the candidate team and a bounded
single-agent baseline. Captain evaluates both result sets independently and
may promote only the candidate team's immutable package.

## Scope

The gate is inserted after `execute_team` and before `evaluate_team` can issue
`PROMOTE_CANDIDATE`:

```text
discover -> brief_codex -> execute_team
  -> business benchmark (candidate + single-agent baseline)
  -> deterministic evaluation -> improve_team or report_captain
  -> Captain/Gateway promotion
```

It extends, rather than replaces, the current private-holdout and technical
assertion checks. Existing technical failures, missing required `TODO_TOOL.v1`
gaps, missing tool evidence, or failed recovery evidence still block before a
business comparison can promote a package.

## Case Sets

Each profile has a Captain-owned `BusinessBenchmarkSuiteV1`, stored privately
and addressed only by an opaque reference and SHA-256 in public evidence.
The suite contains 12--20 cases, with each category represented at least
twice:

1. ordinary, complete cases;
2. boundary cases that need a policy distinction;
3. incomplete cases that require clarifying information;
4. contradictory cases that require conflict handling; and
5. mandatory-escalation cases where autonomous completion is unsafe.

Every `BusinessBenchmarkCaseV1` contains a stable case ID, redacted input,
profile ID, expected structured decision, required rationale facts, allowed
tool-intent set, expected human-handoff flag, and severity. The body and
expected decision never enter Minibook, ordinary Gateway events, agent
prompts outside the leased execution envelope, or LLM-judge prompts.

Claims cases score claim triage, document completeness, safe coverage
handling, and escalation. Renewal cases score risk classification, next-best
action, approved n8n-tool intent, and the required commercial human handoff.

## Baseline and Controlled Execution

For every case Captain creates two separately fenced execution requests:

- **Candidate run:** the released Hermes/AutoGen team and its approved leased
  tools;
- **Baseline run:** one bounded, versioned single-agent policy with the same
  public task envelope but no multi-agent routing and no extra tool access.

Both requests use identical model family/version, maximum cost, maximum
latency, allowed integrations, redaction policy, and immutable case digest.
The baseline cannot become a publishable capability and is evaluation-only.
Case order is deterministically shuffled from the suite digest and withheld
from both systems until execution starts. A case replay must return its
persisted receipt or fail as a conflict; it must not re-run a provider side
effect.

## Independent Deterministic Evaluator

`BusinessBenchmarkEvaluator` is Captain-owned, pure, and rule-based. It reads
the private case plus the typed terminal receipts and emits only redacted
`BusinessBenchmarkReceiptV1` evidence. It scores each run on:

- decision correctness;
- required-rationale completeness;
- tool-intent correctness and unsupported-tool use;
- mandatory/incorrect/missing human handoff;
- USD cost and elapsed time; and
- terminal status and evidence completeness.

No agent, Hermes skill, candidate package, or qualitative judge may write a
score or a promotion disposition. An optional LLM judge may add a redacted
diagnostic artifact only after the deterministic evaluator completed; its
result cannot compensate for a failed mandatory criterion.

## Acceptance Policy

`BusinessBenchmarkPolicyV1` is versioned with the profile and suite. The
initial policy is deliberately conservative:

| Rule | Required result |
| --- | --- |
| Mandatory escalation | 100% correct; one miss blocks promotion |
| Unsafe/unauthorized tool intent | 0; one occurrence blocks promotion |
| Candidate correctness | at least 90% across the suite |
| Candidate vs baseline correctness | candidate >= baseline |
| Candidate vs baseline completion | candidate >= baseline |
| Candidate cost | no more than 125% of baseline total |
| Candidate latency | no more than 150% of baseline total |
| Evidence completeness | receipt for every candidate and baseline case |

The policy exposes the resulting metric values and policy version in an
immutable `BusinessBenchmarkSummaryV1`. It exposes no private inputs,
expected outcomes, model prose, credentials, endpoints, or raw case bodies.
If the candidate fails correctness, completeness, handoff, tool, cost, or
latency policy, Captain returns `RETRY_BUILD` only for a behavioural failure;
otherwise it emits the existing typed blocked/escalated disposition. The
improvement request binds the failed metric IDs to one of the already defined
candidate components, preventing a generic unbounded rewrite.

## Lifecycle and Authority

`TeamEvaluationV1` gains exactly one required benchmark-summary reference and
the summary must bind the same job, correlation, subject version, attempt,
candidate reference, assertion IDs, suite ID/digest, and policy version. The
Quality Warden skill can read only the redacted summary and receipts, not
private cases. `FactoryFeedbackBuilder` can recommend promotion only if both
the existing technical evaluation and the benchmark policy are successful.

The Factory state machine must reject `CAPABILITY_PROMOTED` if the matching
benchmark is absent, stale, incomplete, has a blocking violation, or is below
baseline. Gateway persists the summary and enforces immutable same-bytes
replay. Captain remains the only actor that writes the terminal decision;
Hermes can only return leased execution evidence and a private candidate.
Minibook receives a read-only projection of the decision and redacted metrics
after Gateway commit.

## Verification

The implementation is complete when deterministic tests prove:

1. suites reject private-content leakage, duplicate IDs, bad categories, and
   incomplete expected outcomes;
2. candidate and baseline share a case envelope while the baseline cannot gain
   team tools or promotion authority;
3. the evaluator detects wrong outcomes, wrong/missing handoffs, unapproved
   tools, missing receipts, cost/latency overruns, and candidate regressions
   below baseline;
4. an all-green Claims suite and an all-green Renewal suite permit the normal
   Captain promotion path;
5. either suite's mandatory escalation miss or unsafe tool use blocks that
   path even if every existing technical assertion passes; and
6. an improvement retry retains previously green business metrics and makes a
   new candidate/suite receipt binding before a new promotion decision.

Live evaluation is opt-in and costs are bounded by the Captain job budget. A
skipped provider, unavailable service, missing credential, or incomplete
receipt is never claimed as a business success.

## Non-goals

- Claiming production-domain accuracy from synthetic or anonymized cases.
- Using an LLM judge as a release authority.
- Giving the baseline unrestricted tools, n8n access, or publication rights.
- Replacing human review for regulated, contractual, or irreversible actions.

# Codex Factory Recovery Design

**Status:** Approved by the user on 2026-07-30

## Context

Factory v18 reached the real Codex implementation boundary and exhausted the
900-second lease. `PowerShellCodexRunner` returned exit code `124`, but the
receipt builder rejected the empty buffered output first and reported `Codex
JSONL evidence is empty`. The detached candidate workspace and prior Hermes
brief exist, yet the current replay contract requires a new immutable suite and
new Hermes calls instead of resuming the interrupted implementation.

Versions v15 through v18 are immutable negative evidence. This change must not
rewrite their suites, replays, workspaces, receipts, or provider traces.

## Considered approaches

1. **Increase the timeout.** Rejected. It does not survive process loss, retains
   no progress, and makes another paid attempt repeat all earlier work.
2. **Create a fresh Factory version after every runtime error.** Rejected for
   infrastructure failures. It needlessly repeats deterministic suite creation
   and Hermes discovery/brief calls, and confuses runtime recovery with a
   behavior-improvement attempt.
3. **Durable journal plus a Captain-authorized resumable phase machine.**
   Selected. It retains truthful terminal evidence, resumes the same candidate
   attempt, and keeps behavioral improvement as a separate lifecycle decision.

## Required lifecycle

The existing `seal_codex_build` operation becomes an internal, monotonic build
lifecycle with three Captain-owned phases:

1. `scaffold_ready`: create or recover the exact detached workspace, verify its
   base revision and authority bindings, materialize the content-addressed
   Captain inputs, and install the candidate-local test harness. This phase is
   deterministic and makes no provider call.
2. `implementation_running` / `implementation_interrupted` /
   `implementation_complete`: run Codex against that same workspace while
   appending each complete stdout JSONL line to a private journal. A timeout or
   controlled cancellation closes an execution receipt but preserves the
   workspace, journal, thread identity, and next resumable phase.
3. `sealed`: only after a successful terminal execution, verify the complete
   `factory-candidate.json`, `candidate.zip`, and `test-evidence.json` set and
   issue the existing Captain-owned build receipt.

Checkpoint transitions are monotonic and bound to the exact job, correlation,
Factory attempt, skill invocation, workspace reference, base revision, Codex
brief digest, and Captain lease. Existing content may only be replayed when all
bindings match byte-for-byte.

## Durable JSONL and terminal evidence

`scripts/codex-session.ps1` streams complete stdout lines as they arrive instead
of buffering until process exit. `PowerShellCodexRunner` consumes stdout and
stderr concurrently, appends each non-empty JSONL line to a private per-session
journal, flushes and fsyncs it, and returns the journal snapshot on every
terminal path.

The untrusted journal stays under the configured private Factory state root. A
session receipt contains only redacted metadata: terminal status, exit code,
journal digest, event count/types, optional Codex thread id, session id,
workspace/base bindings, and timestamps. Prompt bodies and stderr are never
placed in public evidence.

Terminal classification is explicit:

- exit `0` -> `succeeded`;
- exit `124` -> `timed_out`;
- Captain/operator cancellation -> `cancelled`;
- any other non-zero exit -> `failed`.

Empty JSONL is invalid only for an alleged successful run. A timeout with zero
or partial events remains a valid terminal failure receipt and must be reported
as exit `124`, never as `JSONL empty`.

## Resume and Captain authority

Runtime recovery is distinct from behavioral improvement:

- `FactoryImprovementAuthorizationV1` continues to authorize a new candidate
  attempt after a failed evaluation.
- `FactoryRuntimeRetryAuthorizationV1` authorizes one bounded continuation of
  the same Codex build attempt. It binds the Captain request, job/correlation,
  skill invocation, workspace/base revision, interrupted checkpoint and
  terminal receipt digests, next resume ordinal, expiry, and maximum runtime.

The replay store gains a durable `interrupted` state for the Codex seal step.
Only the typed runtime interruption may enter it. A matching, unexpired Captain
runtime authorization can reacquire that exact replay; ordinary dispatch,
stale authorization, changed inputs, changed workspace, or an exhausted resume
budget fail closed. The prior discovery and Codex brief replays are reused and
Hermes is not invoked again.

Codex continuation uses the recorded thread id when available. If no thread id
was emitted before interruption, Captain may authorize a continuation against
the same workspace and prompt contract, but it remains the same Factory attempt
and resume ordinal is recorded. It must not create a new suite or claim a new
behavioral attempt.

## Failure and recovery rules

- A running process found after restart is never duplicated; it requires
  inspection or cancellation before continuation.
- Missing or digest-conflicting checkpoint, journal, workspace, receipt, or
  authorization data fails closed.
- Successful execution with missing artifacts is a failed seal, not a resumable
  provider timeout.
- Seal is idempotent: identical completed evidence replays; conflicting bytes
  are rejected.
- v19 is created only after all local recovery tests and repository gates pass.
- The 30 provider-backed benchmarks run only after v19 produces and seals both
  candidates and only with explicit paid-run authorization.

## Acceptance tests

1. PowerShell emits observable JSONL before the child exits.
2. Runner timeout with zero JSONL returns and persists `124/timed_out`.
3. Runner timeout after partial JSONL preserves valid lines and their digest.
4. Restart recovers the same workspace/checkpoint without recreating inputs.
5. A matching Captain runtime authorization resumes the interrupted seal while
   discovery and brief call counts remain unchanged.
6. Missing, stale, mismatched, replayed, or over-budget authorization fails
   closed.
7. Seal is impossible until all required candidate artifacts pass validation.
8. Existing successful and failed replay behavior remains compatible.
9. Focused recovery tests, architecture tests, compileall, submission check,
   and the full non-live suite pass before v19 preparation.

## Operational boundary

This goal is local and non-paid. It may prepare the v19 immutable inputs and a
dry-run command, but it must not call Hermes, Codex, the 30-case benchmark,
Gateway promotion, or Minibook live projection without a later explicit paid
run authorization.

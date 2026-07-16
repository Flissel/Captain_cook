# Gateway-native delivery runtime design

## Decision

MariaDB-backed gateway blocks remain Captain Cook's only delivery authority.
Consumers replay immutable blocks by ledger index and persist only their own
operational cursor. No SQLite runtime state, second command log, outbox, or
broker is introduced.

## Architecture

```text
Captain -> gateway work_batch / holdout -> MariaDB immutable blocks
worker -> claim-fenced Codex, evidence, validation events -> gateway
Captain -> recovery and review decisions -> gateway
Captain projectors <- ordered gateway index feed -> Minibook / Hermes read models
```

`GET /events?after_index=<n>&limit=<1..100>` returns a strictly increasing
page of authenticated blocks and `next_after_index`. A Captain-owned projector
checkpoints with `PUT /consumers/{consumer_name}/cursor`, using a mutable
`gateway_consumer_cursors` row with a unique consumer name, monotonic index,
and compare-and-set `expected_index`. Cursor rows are operational offsets, not
delivery truth; deleting one only causes a safe replay.

The existing ledger index already provides transactional ordering and replay.
An outbox would duplicate that responsibility; an external broker is out of
scope for the deterministic local runtime.

## Event and ownership contract

Every worker event is a child of its claimed `work_batch`, has matching
`batch_id` and current `iteration`, and uses the existing claim-token fence.

| Type | Owner | Required payload | Meaning |
|---|---|---|---|
| `codex_session` | worker | existing session fields | session creation proof |
| `codex_process` | worker | `batch_id`, `iteration`, `process_id`, `state`, `command_digest` | sanitized process state |
| `reasoning_slice` | worker | `batch_id`, `iteration`, `slice_id`, `summary_ref`, `sha256` | opaque artifact reference, never chain-of-thought |
| `recovery_decision` | Captain | `batch_id`, `iteration`, `reason`, `decision` | requeue or infrastructure termination audit |
| `validation_run` | worker | existing validation evidence | independent validation |
| `review_decision` | Captain | `batch_id`, `iteration`, `review_id`, `decision`, `evidence_refs` | independent review verdict |
| `batch_done` | worker | existing terminal outcome | terminal only after required evidence |

New payloads are strict Pydantic contracts. They reject raw command output,
workspace paths, bearer tokens, claim tokens, and secret environment values.
`recovery_decision` and `review_decision` are Captain-only; other execution
events are worker-only and claim-fenced. Generic `/blocks` must enforce those
rules rather than bypass them.

## D02-D05 boundaries

- D02 owns `agenten/execution/` Codex supervision: injected argument-vector
  runner, workspace guard, allowlisted environment, session/process events,
  and typed artifacts.
- D03 owns recovery and reasoning references: stale claims are read from the
  gateway and resolved through immutable Captain decisions, never local state.
- D04 owns evidence/review: success needs current-iteration validation plus a
  passing independent review; five recorded failures end as
  `failed_after_max_iterations`.
- D05 owns feed/cursors and Minibook/Hermes projections. Effects use
  `gateway:<consumer>:<index>` idempotency keys, then advance the cursor. A
  projection never creates, approves, claims, or completes work.

## Failure rules

Gateway outage produces a sanitized error and never a local completion. Worker
crashes are recovered from lease expiry. A projector crash after an effect
replays the same index; the idempotency key prevents a duplicate visible
effect. Cursor compare-and-set races return `409` and require reread/replay.
Invalid review or validation evidence cannot produce terminal success.

## Verification and non-goals

Each D02-D05 packet needs focused HTTP-contract tests, disposable-MariaDB
tests with zero skips, import/architecture checks, and replay/failure tests.
P20 alone owns live Codex, Hermes, Minibook, n8n, and Mailpit evidence. This
design neither runs those services nor changes VibeMind-owned n8n resources.

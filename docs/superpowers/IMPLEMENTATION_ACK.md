# Captain Cook implementation ACK and control board

Only the orchestrator edits this file. Workers receive `HANDOFF TO WORKER <ID>`
from the worker-goal document and return `HANDOFF FROM WORKER <ID>` in chat or
agent messaging. This avoids shared-file conflicts between worker branches.

## Control state

```text
LOOP_STATE: REVIEWING_CANDIDATES
MAX_PARALLEL_WORKERS: 3
SCHEDULE_STATE: PENDING_USER_CONFIRMATION_AND_NATIVE_UI
PROPOSED_SCHEDULE: EVERY_30_MINUTES_WHILE_ACTIVE
INTEGRATION_WORKTREE: C:\Users\User\Desktop\Captain_cook-main-integration
INTEGRATION_BRANCH: feat/system-remediation-orchestration
LAST_INTEGRATED_IMPLEMENTATION_SHA: 11febc37
CONTROL_DISPATCH_SHA: 4601806
PRIMARY_WORKTREE_POLICY: PRESERVE_AND_DO_NOT_USE_FOR_INTEGRATION
ACK_OWNER: ORCHESTRATOR_ONLY
```

`4601806` contains this ACK, the worker prompts, and the current master-plan
TODOs. It is the exact base for the current P06/P09 wave. A later orchestrator
may advance the base only after recording a new explicit dispatch SHA here.

## Completed packets

| Packet | Candidate | Integration | Verified evidence |
|---|---:|---:|---|
| P00 | `6374338` | `6374338` | program/architecture baseline |
| P01 | `e16aec0` | `b607128` | reproducible environment gate |
| P03 | `45ea024` | `d400d9e` | isolated 22-test DB/gateway gate |
| P03A | `854c7e1` | `ed7c977` | extensible DB gate and full suite |
| P04 | `1f09a30` | `4652fb8` | 50 Pester plus architecture/import gates |
| P05 | `4a2ee1f` | `11febc37` | spec PASS, quality PASS, 64 Pester, both Compose renders, parser PASS |
| P07A | `46dfc80` | `e951549` | spec/quality PASS, 47 focused tests |

P05 closed all known fail-open bootstrap result-shape gaps. P06 is unblocked.

## Active findings and stop gates

P07B candidate `225bbe9` implements the six prior quality findings, but the
fresh specification review is FAIL because the named Claim/Holdout lock-order
regression has no real MariaDB concurrency test. Repair attempt 1/3 is active;
the candidate must not integrate until the bounded deadlock/hash/immutability
test and a fresh spec plus quality review pass.

1. concurrent duplicate root batches;
2. stale-snapshot `previous_hash` chain break;
3. Claim/Holdout lock-order inversion and incomplete write retries;
4. forged gateway-owned claim/heartbeat events through generic `/blocks`;
5. invalid work-batch root status persisted before projection validation;
6. a second holdout logically replacing the immutable suite.

P06 candidate `5db62f8` is specification FAIL: the real preflight ignores its
configuration and treats every occupied fixed port as foreign, breaking healthy
reruns and external-n8n mode. Repair attempt 1/3 is active.

P09 candidate `dc332fb` is specification PASS and quality FAIL: holdout isolation
is only batch-local, partial release can precede a later policy failure, and
capability tags are not canonicalized before resolver/release. Repair attempt
1/3 is active.

P07C remains blocked by P07B. P08 remains blocked by P07C. P10 remains blocked
by P09.

## Current dispatch board

```text
HANDOFF TO WORKER 1: P07B-FIX
STATE: REPAIR_ATTEMPT_1_IN_PROGRESS
LOCK: LOCK_GATEWAY
BRANCH: refactor/gateway-append-only-store
WORKTREE: C:\Users\User\Desktop\Captain_cook\.worktrees\gateway-append-only-store
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-1--p07b-fix
CANDIDATE_SHA: 225bbe959b4cf5db311c9e45345d4b5b30b6e997
WORKER_GATE: 45 focused passed; full gateway gate 336 passed, 1 allowlisted skip, 1 deselected
WORKER_WARNING: Starlette/httpx compatibility warning remains P20-owned
SPEC_REVIEW: FAIL - missing real Claim/Holdout concurrency regression evidence

HANDOFF TO WORKER 2: P06
STATE: REPAIR_ATTEMPT_1_IN_PROGRESS
LOCK: LOCK_LIFECYCLE
BRANCH: fix/setup-preflight-contract
WORKTREE: C:\Users\User\Desktop\Captain_cook\.worktrees\setup-preflight-contract
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-2--p06
CANDIDATE_SHA: 5db62f82a2d6401db1b57a246e1c4e0fa78eba6c
WORKER_GATE: 15 focused and 73 full Pester tests passed; parser and diff checks passed
WORKER_LIMIT: live winget/PATH acceptance intentionally not claimed
SPEC_REVIEW: FAIL - production preflight is configuration/ownership blind

HANDOFF TO WORKER 3: P09
STATE: REPAIR_ATTEMPT_1_IN_PROGRESS
LOCK: LOCK_PLANNING
BRANCH: feat/captain-planning-policy
WORKTREE: C:\Users\User\Desktop\Captain_cook\.worktrees\captain-planning-policy
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-3--p09
CANDIDATE_SHA: dc332fb40e89d7a8cf2bc41d95e80adee0cff7f8
WORKER_GATE: 9 focused plus 12 architecture/import tests passed; compileall passed
WORKER_LIMIT: integrated full suite must rerun after the P07B basename-collision fix lands
SPEC_REVIEW: PASS
QUALITY_REVIEW: FAIL - run-wide holdout isolation and canonical tags required

QUEUE: P15
STATE: READY_WHEN_SLOT_RETURNS
LOCKS: LOCK_RUNTIME_CORE plus the P15-owned recorder/adapter paths
```

P02 is technically ready but belongs to the adjacent Minibook product and is
not part of the recommended three-worker Captain-Core wave.

The objective-required root `input.md` and `plans/index.md` were absent at the
`4601806` dispatch baseline. They are now drafted with subordinate requirements,
architecture, implementation, and test specs. Their presence closes the file
gap only; AF00/AF01 review is still required before Agent-Factory implementation
packets may be dispatched.

The worktree `.worktrees/remediation-integration` on
`integration/remediation-commits` is observed but not owned by this loop. Treat
it as foreign state and preserve it unless the user explicitly assigns it.

## Orchestrator loop protocol

1. Verify Git/worktree state and resolve actual SHAs.
2. Read all returned `HANDOFF FROM WORKER` blocks.
3. Audit scope, commit, tests, skips, warnings, and service safety.
4. Run specification review, then quality review.
5. Return findings to the owning worker; update this ACK from verified evidence.
6. Simulate integration and inspect overlap before merging.
7. Run the integrated packet gate and commit plan/ACK evidence separately.
8. Recompute ready/blocked packets and rewrite the dispatch board.
9. Record every new gap as an unchecked plan checkbox.

## Safety state

- Use only disposable `captain-cook-test` resources for database gates.
- Never run `docker compose down -v` or touch VibeMind n8n/volumes.
- The P05 gate rendered Compose without starting or stopping services.
- The last P07B gate left no test containers or `.coverage` artifact and
  preserved protected-service start times.
- Requests and Starlette/httpx warnings are known P20 work, not silent passes.
- Live LLM, browser, MCP, deployment, and clean-clone claims require their own
  later real-evidence packets.
- Native Codex Scheduled could not be created from this session because no
  automation-update capability is registered. The proposed 30-minute cadence
  still needs user confirmation and activation in the Scheduled UI.
- The connected n8n instance exposes ten workflow cards, all with MCP details
  disabled. `captain-gate-a-mailpit` cannot be reused or rejected until its
  sanitized export or MCP-visible contract is available.

## ACK return schema

```text
HANDOFF FROM WORKER <ID>
Packet:
Dispatch SHA:
Branch/worktree:
Candidate SHA:
Exact changed paths:
RED evidence:
GREEN focused evidence:
Full gate:
Skipped tests and warnings:
Service/resource safety evidence:
Specification review:
Quality review:
Remaining risks/TODOs:
Integration performed: no
END HANDOFF FROM WORKER <ID>
```

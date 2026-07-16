# Captain Cook implementation ACK and control board

Only the orchestrator edits this file. Workers receive `HANDOFF TO WORKER <ID>`
from the worker-goal document and return `HANDOFF FROM WORKER <ID>` in chat or
agent messaging. This avoids shared-file conflicts between worker branches.

## Control state

```text
LOOP_STATE: READY_FOR_DISPATCH
MAX_PARALLEL_WORKERS: 3
SCHEDULE_STATE: PENDING_USER_CONFIRMATION
PROPOSED_SCHEDULE: EVERY_30_MINUTES_WHILE_ACTIVE
INTEGRATION_WORKTREE: C:\Users\User\Desktop\Captain_cook-main-integration
INTEGRATION_BRANCH: feat/system-remediation-orchestration
LAST_INTEGRATED_IMPLEMENTATION_SHA: 11febc37
PRIMARY_WORKTREE_POLICY: PRESERVE_AND_DO_NOT_USE_FOR_INTEGRATION
ACK_OWNER: ORCHESTRATOR_ONLY
```

The commit containing this ACK and the current master-plan TODOs becomes the
next exact dispatch SHA for new P06/P09/P15 worktrees. Resolve it with Git at
dispatch time; do not copy `11febc37` as their base automatically.

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

P07B candidate `ad001ac` has specification PASS and prior real-gate evidence,
but quality review is FAIL. It must not integrate until all six master-plan
checkboxes close:

1. concurrent duplicate root batches;
2. stale-snapshot `previous_hash` chain break;
3. Claim/Holdout lock-order inversion and incomplete write retries;
4. forged gateway-owned claim/heartbeat events through generic `/blocks`;
5. invalid work-batch root status persisted before projection validation;
6. a second holdout logically replacing the immutable suite.

P07C remains blocked by P07B. P08 remains blocked by P07C.

## Current dispatch board

```text
HANDOFF TO WORKER 1: P07B-FIX
STATE: READY
LOCK: LOCK_GATEWAY
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-1--p07b-fix

HANDOFF TO WORKER 2: P06
STATE: READY_AFTER_CONTROL_COMMIT
LOCK: LOCK_LIFECYCLE
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-2--p06

HANDOFF TO WORKER 3: P09
STATE: READY_AFTER_CONTROL_COMMIT
LOCK: LOCK_PLANNING
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-3--p09

QUEUE: P15
STATE: READY_WHEN_SLOT_RETURNS
LOCKS: LOCK_RUNTIME_CORE plus the P15-owned recorder/adapter paths
```

P02 is technically ready but belongs to the adjacent Minibook product and is
not part of the recommended three-worker Captain-Core wave.

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

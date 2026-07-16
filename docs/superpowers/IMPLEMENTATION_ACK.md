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
  LAST_INTEGRATED_IMPLEMENTATION_SHA: df79012
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
| P15 | `e688177` | `61b1858` | spec/quality PASS, 54 integrated tests, compileall PASS |
| P09 | `51cfb69` | `df79012` | spec/quality PASS, explicit AF01 reconciliation, 123 combined tests |

P05 closed all known fail-open bootstrap result-shape gaps. P06 is unblocked.

## Active findings and stop gates

P07B candidate `6acee13` is integrated as merge commit `848c83f`. The focused
real MariaDB gate passed 46 tests with zero skips and the complete integrated
gate passed 391 tests with two explicit skips, one live deselection, one known
warning, and 81.79% coverage. Fresh specification and quality reviews passed
with no Critical/Important findings; the default pytest basename collision is
closed by the canonical no-content gateway test rename.

1. concurrent duplicate root batches;
2. stale-snapshot `previous_hash` chain break;
3. Claim/Holdout lock-order inversion and incomplete write retries;
4. forged gateway-owned claim/heartbeat events through generic `/blocks`;
5. invalid work-batch root status persisted before projection validation;
6. a second holdout logically replacing the immutable suite.

P06 candidate `5db62f8` is specification FAIL: the real preflight ignores its
configuration and treats every occupied fixed port as foreign, breaking healthy
reruns and external-n8n mode. Repair attempt 1/3 is active.

P09 candidate `51cfb69` is integrated with AF01 as `df79012` after explicit
resolution of all four overlapping paths. Specification and quality reviews
are PASS; the combined 123-test gate and `compileall` passed. The planning lane
is unlocked for P10.

P07C is unlocked by P07B and is the active `LOCK_GATEWAY` packet. P08 remains
blocked by P07C. P10 is independently unlocked by the completed P09 packet.

### Autonomous Captain process candidate

```text
PACKET: AF01-PROCESS-BOUNDARIES
STATE: INTEGRATED_CONTRACT_EVIDENCE_ONLY
WORKTREE: C:\Users\User\Desktop\Captain_cook-main-integration
SOURCE_INPUT: C:\Users\User\Desktop\Captain_cook\Autogen_AgentFarm\input.md
SOURCE_SHA256: e55e667474a3b6a3d1a1dc6f927fec9ea67a247ea30ea61141c5b994495623ac
WORKERS_USED: 6
FOCUSED_GATE: 123 combined AF01/P09/P15 tests plus agenten compileall
DEFAULT_FULL_GATE: PASS - 391 passed, 2 skipped, 1 deselected, 1 warning, 81.79% coverage
SKIPS: 1 no-autogen degradation path; 1 explicit ledger-query live marker
WARNING: Starlette/httpx deprecation
SUBMISSION_VERIFIER: passed
OFFLINE_DEMO: 4 subproblems reached done; temporary output only
LIVE_INTEGRATION_EVIDENCE: none
INTEGRATION_PERFORMED: yes - df790121061e181edd220ab6e3d8380d60ca4bee
FINAL_DIFF_REVIEW: no Critical/Important findings in the implemented local scope; gateway authority and OS sandbox remain explicit production gates
```

The candidate separates Parser/Planning/Review/Execution contracts and is
documented in
`docs/superpowers/plans/2026-07-16-autonomous-captain-processes.md`. It must not
be treated as complete v2 evidence: gateway-backed authority readers, separate
review sandboxing, Hermes/Codex execution, real n8n/AutoGen runtime composition,
and real E2E release gates remain open. The local compiler now accepts an
allowlisted mixed `n8n`/`autogen` DAG, but that is contract evidence rather than
live runtime proof. Because this work overlaps `LOCK_PLANNING`,
the orchestrator must reconcile it with P09 before any integration commit.

## Current dispatch board

```text
HANDOFF TO WORKER 1: P07B-FIX
STATE: INTEGRATED
LOCK: LOCK_GATEWAY
BRANCH: refactor/gateway-append-only-store
WORKTREE: C:\Users\User\Desktop\Captain_cook\.worktrees\gateway-append-only-store
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-1--p07b-fix
CANDIDATE_SHA: 6acee13
WORKER_GATE: 46 selected passed, zero skips; full 337 passed, 1 allowlisted skip, 1 deselected; 81.79% coverage
WORKER_WARNING: Starlette/httpx compatibility warning remains P20-owned
SPEC_REVIEW: PASS
QUALITY_REVIEW: PASS
INTEGRATION_SHA: 848c83f28e0c3292d159c78af1613aef6e5e38e8
INTEGRATED_GATE: 46 selected passed, zero skips; full 391 passed, 2 explicit skips, 1 deselected, 1 warning; 81.79% coverage

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
STATE: INTEGRATED
LOCK: LOCK_PLANNING
BRANCH: feat/captain-planning-policy
WORKTREE: C:\Users\User\Desktop\Captain_cook\.worktrees\captain-planning-policy
PROMPT: docs/superpowers/prompts/2026-07-16-worker-goals.md#handoff-to-worker-3--p09
CANDIDATE_SHA: 51cfb69cb92605412a0dc204b5092f0733836790
WORKER_GATE: 17 focused plus 15 architecture/import/workstream tests passed; compileall passed
WORKER_LIMIT: integrated full suite must rerun after the P07B basename-collision fix lands
SPEC_REVIEW: PASS
QUALITY_REVIEW: PASS
INTEGRATION_SHA: df790121061e181edd220ab6e3d8380d60ca4bee
INTEGRATED_GATE: 123 passed; agenten compileall passed

HANDOFF TO WORKER 4: P15
STATE: INTEGRATED
LOCKS: LOCK_RUNTIME_CORE plus the P15-owned recorder/adapter paths
BRANCH: refactor/event-bus-capabilities
WORKTREE: C:\Users\User\Desktop\Captain_cook\.worktrees\event-bus-capabilities
DISPATCH_SHA: 3136d0d728ba77ee9467bf5cbb9da69af48c3436
CANDIDATE_SHA: e6881774ad075d928517ff797c6acfb4f6944441
RED_GATE: 4 failed, 2 passed, 25 deselected on the old capability mismatch
GREEN_GATE: 54 focused passed; agenten compileall passed
DIAGNOSTIC_FULL_GATE: 294 passed, 24 skipped, 1 deselected, 1 known warning
SPEC_REVIEW: PASS
QUALITY_REVIEW: PASS
INTEGRATION_SHA: 61b1858a56dd570941c85bc996c2131cfaab44d3
INTEGRATED_GATE: 54 passed, 0 skipped; compileall passed
```

P02 is technically ready but belongs to the adjacent Minibook product and is
not part of the recommended three-worker Captain-Core wave.

The objective source `Autogen_AgentFarm/input.md` is committed on the clean
sibling repository at `9ac6fe9` (blob `02ea2c8`, SHA-256 matching the AF01
record). The root `input.md` and `plans/index.md` were absent at the `4601806`
dispatch baseline and are now drafted with subordinate requirements,
architecture, implementation, and test specs. This closes source tracking and
local contract-file gaps only; it is not live Agent-Factory evidence.

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

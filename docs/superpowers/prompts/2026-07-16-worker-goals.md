# Current worker goal prompts

The orchestrator copies one marked block into one isolated worker session. The
worker returns the ACK format at the end. Run no more than three blocks at once.

## HANDOFF TO WORKER 1 — P07B-FIX

```text
Goal: Harden existing P07B candidate
ad001ac96089e6efa857b930a31f31f64cf0646d on branch
refactor/gateway-append-only-store until every P07B quality-hardening checkbox
in the master program plan is closed with real MariaDB evidence.

Use the existing gateway-append-only-store worktree. Read AGENTS.md, the master
plan, Gateway Task 2, and IMPLEMENTATION_ACK.md before editing. Allowed paths:
gateway/store.py, gateway/app.py, tests/gateway/test_gateway.py,
tests/blockchain/test_mariadb_storage.py. Preserve the completed blob-identical
test-contract rename.

Use TDD. Required behavior: concurrent work_batch creation produces exactly one
root per batch_id; concurrent writes preserve immediate previous_hash adjacency;
all affected writers use one lock order and bounded retry only for documented
MariaDB transaction errors; generic POST /blocks rejects gateway-owned claim,
heartbeat, and approval event types; invalid initial work-batch status is
rejected before insert; a second holdout cannot replace the effective immutable
suite. Preserve append-only parents, current-iteration fencing, validation-
before-success, digest-only token persistence, and holdout secrecy.

Run focused RED/GREEN tests, then pwsh -NoProfile -File
scripts/test_gateway.ps1. Use only captain-cook-test resources, verify cleanup
and protected-service start times, and never run docker compose down -v. Commit
narrowly with a Conventional Commit. Do not merge/rebase/push or edit plans.

Return exactly:
HANDOFF FROM WORKER 1
Packet: P07B-FIX
Dispatch SHA:
Candidate SHA:
Exact changed paths:
RED evidence:
GREEN focused evidence:
Full gate:
Skipped tests/warnings:
Resource safety evidence:
Remaining risks:
Integration performed: no
END HANDOFF FROM WORKER 1
```

## HANDOFF TO WORKER 2 — P06

```text
Goal: Implement P06 Aggregate Windows Preflight from System Task 4 on branch
fix/setup-preflight-contract, starting from the exact dispatch SHA supplied by
the orchestrator after integrated P05.

Create/use an isolated worktree. Allowed paths: setup.ps1,
scripts/setup/Preflight.psm1, scripts/setup/Lifecycle.psm1,
scripts/setup/Setup.Tests.ps1. Implement Test-SetupPreflight and
Confirm-InstalledPrerequisite, aggregate all preflight results with the planned
severity order, wire the real Preflight stage to the aggregate, and return
RestartRequired when a newly installed executable is not visible. Preserve
P04/P05 parameter positions, exports, checkpoint behavior, strict result
contracts, and external-n8n ownership.

Use TDD with version, port, missing executable, aggregate, and restart-required
cases. Tests use injected providers and must not install software, kill port
owners, start services, or mutate user config. Run the complete Setup.Tests.ps1
Pester suite, PowerShell parser, and git diff --check. Commit narrowly with a
Conventional Commit. Do not merge/rebase/push or edit plans.

Return exactly:
HANDOFF FROM WORKER 2
Packet: P06
Dispatch SHA:
Candidate SHA:
Exact changed paths:
RED evidence:
GREEN focused evidence:
Full gate:
Skipped tests/warnings:
Safety evidence:
Remaining risks:
Integration performed: no
END HANDOFF FROM WORKER 2
```

## HANDOFF TO WORKER 3 — P09

```text
Goal: Implement P09 Deterministic Captain Planning Policy from Captain Task 2
on branch feat/captain-planning-policy, starting from the exact orchestrator
dispatch SHA in an isolated worktree.

Allowed paths: agenten/planning/policy.py,
agenten/planning/captain_pipeline.py, agenten/planning/factory.py,
tests/planning/test_policy.py, tests/planning/test_captain_pipeline.py. Create
typed PlanningPolicy/PlanningPolicyError, reject capabilities outside the
configured vocabulary, fingerprint canonical case JSON excluding case_id, and
reject content overlap between visible golden cases and hidden holdouts.
Validate immediately after enrichment and build the policy from
known_capability_tags.

Use TDD. Cover canonical ordering, different IDs with equal content, nested
JSON, unknown tags, and valid input. Run the policy/pipeline/factory E2E gate,
relevant architecture/import checks, and compileall for agenten/planning.
Commit narrowly with a Conventional Commit. Do not edit gateway, delivery,
shared docs, or plans; do not merge/rebase/push.

Return exactly:
HANDOFF FROM WORKER 3
Packet: P09
Dispatch SHA:
Candidate SHA:
Exact changed paths:
RED evidence:
GREEN focused evidence:
Full gate:
Skipped tests/warnings:
Remaining risks:
Integration performed: no
END HANDOFF FROM WORKER 3
```

## QUEUED HANDOFF — P15

P15 is ready but queued until a worker slot returns. Its canonical source is
System Task 7 and the approved event-bus capability-segregation design. When
activated, replace a completed worker block in ACK with a full P15 handoff; do
not run it concurrently with P16 or P17.

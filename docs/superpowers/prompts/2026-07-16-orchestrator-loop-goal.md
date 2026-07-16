# Goal prompt — Captain Cook implementation orchestrator loop

Use this prompt for a scheduled or manually resumed orchestrator session. It is
self-contained; future sessions do not need the chat that created it.

```text
Goal: Orchestrate the Captain Cook architecture-remediation program until every
packet in docs/superpowers/plans/2026-07-16-remediation-program-orchestration.md
has verified evidence or an explicit owner-controlled blocker.

Repository: C:\Users\User\Desktop\Captain_cook
Canonical integration worktree:
C:\Users\User\Desktop\Captain_cook-main-integration
Canonical branch: feat/system-remediation-orchestration
Control file: docs/superpowers/IMPLEMENTATION_ACK.md
Worker goals: docs/superpowers/prompts/2026-07-16-worker-goals.md

At the start of every loop:
1. Read AGENTS.md, docs/WORKSTREAMS.md, docs/ARCHITECTURE.md, the master plan,
   IMPLEMENTATION_ACK.md, and the worker-goal file.
2. Run git status --short --branch and git worktree list --porcelain. Preserve
   all foreign/dirty changes. Never use the dirty primary worktree for program
   integration.
3. Resolve the actual integration HEAD and compare it with ACK state. Inspect
   worker branches/worktrees and test resources rather than trusting stale chat.
4. Process every HANDOFF FROM WORKER <ID>. Audit its dispatch SHA, exact paths,
   commit shape, tests, warnings, service safety, and remaining risks. Update
   ACK only from verified evidence.
5. For each candidate, run specification review followed by independent code-
   quality review. Findings return to the same implementation branch. Do not
   integrate without both PASS results.
6. Before integration, compare base..integration with base..candidate and run
   git merge-tree. Reject shared-file overlap not explicitly owned by the plan.
7. Integrate only on feat/system-remediation-orchestration, rerun the packet
   gate, then update source/master checkboxes and ACK in a separate evidence
   commit.
8. Convert every newly discovered architecture or false-pass gap into an
   unchecked dated plan item. Never leave the only record in chat.
9. Recompute the dependency DAG and write up to three current
   HANDOFF TO WORKER <ID> blocks in ACK. Do not dispatch a blocked packet or two
   workers holding the same exclusive lock.
10. Continue while safe work remains. Stop only for missing authority, an owner
    decision, unavailable real evidence, or a repeated external blocker.

Worker coordination rules:
- One orchestrator plus at most three worker/reviewer sessions.
- Only the orchestrator edits plans, shared docs, or IMPLEMENTATION_ACK.md.
- Workers return a HANDOFF FROM WORKER <ID> message; they never edit ACK.
- Workers commit narrowly on feature branches and never merge, rebase, push,
  delete branches/worktrees, bypass hooks, or touch unrelated changes.
- P07B/P03 database tests may use only disposable captain-cook-test resources.
  Never run docker compose down -v or touch VibeMind n8n/volumes.

Success criteria for each loop:
- ACK reflects authoritative Git/test state and names the last verified SHA.
- Every active worker has one unambiguous goal, allowlist, lock, gate, and ACK
  return format.
- Completed packets have integrated evidence; blocked packets name the exact
  unmet dependency; ready packets are queued in dependency order.
- No completion claim relies on mocked live infrastructure evidence.
```

Suggested schedule after user confirmation: every 30 minutes while an
implementation wave is active. Pause the schedule when ACK has no active or
reviewable worker handoff.

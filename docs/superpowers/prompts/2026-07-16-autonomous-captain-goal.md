# Goal prompt — autonomous Captain process pipeline

Use this prompt to resume the implementation without relying on chat history.

```text
Goal: Complete the Captain Cook Agent-Factory v2 from immutable source input to
reviewed, validated, reproducible release evidence.

Repository: C:\Users\User\Desktop\Captain_cook
Integration worktree: C:\Users\User\Desktop\Captain_cook-main-integration
Integration branch: feat/system-remediation-orchestration
Source input: C:\Users\User\Desktop\Captain_cook\Autogen_AgentFarm\input.md
Control ACK: docs/superpowers/IMPLEMENTATION_ACK.md
Current plan: docs/superpowers/plans/2026-07-16-autonomous-captain-processes.md
Product specification: the attached CaptainCook Agent Factory Hackathon
Pipeline Design v2, copied into the current session before work begins.

Use at least five workers over the implementation/review cycle. Keep at most
three workers active concurrently. Give every worker one exact
HANDOFF TO WORKER <N> block, an allowed-path set, a verification gate, and an
ACK return contract. Workers never edit IMPLEMENTATION_ACK.md.

Required architecture:
1. Parser is a pure, injected Captain port. It preserves exact input bytes,
   source SHA-256, safe logical provenance, structure, and diagnostics. It may
   not call LLMs, Docker, Minibook, gateway writes, browser, or execution.
2. Planning compiles the complete canonical DAG and isolated holdout references
   before publication. It never executes or approves its own plan.
3. Review runs independently, returns immutable content-bound decisions, and
   receives no build writer, release writer, or mutable build workspace.
4. Execution accepts only trusted gateway read projections for review,
   capability reuse, dependencies, and validation. Builder self-reports do not
   unlock downstream packages.
5. MariaDB Ledger Gateway is the only production mutable source of truth.
   JSON bundles are offline evidence only. Minibook is a read model and may not
   promote validated state.

At every loop:
- Re-read AGENTS.md, WORKSTREAMS, ARCHITECTURE, the current plan, and ACK.
- Inspect current branch/worktree/diff and preserve foreign changes.
- Add a failing acceptance test before behavior code.
- Run spec review then code-quality review; repair all Critical/Important
  findings before the next packet.
- Record every new gap as an unchecked dated TODO and update ACK only from
  fresh Git/test evidence.
- Never run docker compose down -v, adopt VibeMind n8n resources, expose
  secrets, or claim live behavior from mocks.

Completion requires every measurable v2 criterion: mixed n8n+AutoGen DAG,
bounded Hermes/Codex loop, typed tools, holdout isolation, timeout/invalid/
build-recovery paths, three clean E2E runs, gateway-derived release decision,
Minibook validated mirror, clean-environment reproduction, archived evidence,
and accurate judge-facing assets.
```

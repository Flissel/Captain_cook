---
name: captain-factory-brief-codex
description: Create a bounded Codex build assignment for a validated Factory inventory. Use when a Tool Integrator lease must delegate one dependency-ready work unit with release-bound evidence.
---

# Brief Codex

Use only the supplied `captain.factory-skill-invocation.v1`. Verify the job,
released skill digest, input digest, active role lease, attempt, and idempotency
key before any effect. Return exactly the declared typed artifact. Stop on stale
authority, digest mismatch, terminal state, missing required evidence, or an
effect outside the lease. Never publish a skill, write the ledger, weaken Captain
assertions, expose secrets, or claim ready_to_use.

1. Select the smallest dependency-ready work node and write its goal, measurable
   outcome, authorized worktree and paths, architecture constraints, test IDs,
   tool policy, model/budget limits, and evidence requirements using the
   [assignment template](templates/codex-assignment.md).
2. Delegate only through `codex.run`. Use digest-bound `codex.resume` only for
   the same prompt and session digest.
3. For build work, load and follow `plan` and `test-driven-development`. Load
   `systematic-debugging` only after a diagnosed failure and only for its repair
   step. Load `requesting-code-review` before completion.
4. Only the Tool Integrator may use a separate short-lived `n8n-builder` profile
   and `n8n-mcp` lease when the compiled specification declares
   `integration_intent=n8n`. Limit the assignment to an isolated draft; never
   allow activation, production adoption, service administration, or volume
   management.
5. Require code, tests, manifests, digests, and command evidence; seal candidate
   inputs and outputs before validation. Do not grant an approval bypass.
6. Return `CodexBuildBriefV1`; retain prompt/context digests rather than raw
   secret-bearing bodies.

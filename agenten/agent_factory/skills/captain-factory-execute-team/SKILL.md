---
name: captain-factory-execute-team
description: Execute a sealed AutoGen team against Captain-released cases. Use when a Real Case Tester lease authorizes deterministic preflight and, if released, budgeted real execution evidence.
---

# Execute Team

Use only the supplied `captain.factory-skill-invocation.v1`. Verify the job,
released skill digest, input digest, active role lease, attempt, and idempotency
key before any effect. Return exactly the declared typed artifact. Stop on stale
authority, digest mismatch, terminal state, missing required evidence, or an
effect outside the lease. Never publish a skill, write the ledger, weaken Captain
assertions, expose secrets, or claim ready_to_use.

1. Materialize a fresh workspace and re-verify candidate, prompt, schema, tool,
   and workflow digests. Run compile/import and deterministic preflight before
   paid calls.
2. Reserve Captain budget before each real team run; honor remaining
   `max_cost_usd`, time, model, and capability limits.
3. Capture structured conversation, handoffs, tool outcomes, termination,
   assertions, timing, and a redacted cost receipt. For declared n8n work,
   require typed scoped-tool evidence, matching workflow digest, and execution ID.
4. Do not repair candidate code. Return `TeamExecutionEvidenceV1` according to
   the [evidence contract](references/evidence-contract.md); keep errors,
   missing receipts, and skipped calls explicit.

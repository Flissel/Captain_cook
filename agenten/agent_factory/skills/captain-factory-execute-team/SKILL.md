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
   `max_cost_usd`, time, model, and capability limits. If
   `live_execution=false`, forbid every provider, browser, computer-use, n8n,
   or other billable effect. Stop before a paid call on a missing reservation.
   Treat unknown or contradictory cost as unresolved failure. On
   `BUDGET_EXHAUSTED`, stop all new paid effects.
3. Capture structured conversation, handoffs, tool outcomes, termination,
   assertions, timing, and a redacted cost receipt. Permit n8n only when the
   compiled specification declares `integration_intent=n8n` and the lease is
   short-lived with the `n8n-builder` profile and `n8n-mcp` scope. Limit work to
   an isolated draft and typed evidence with matching workflow digest and
   execution ID. Never allow activation, production adoption,
   service administration, or volume management.
4. Do not repair candidate code. Return `TeamExecutionEvidenceV1` according to
   the [evidence contract](references/evidence-contract.md); keep errors,
   missing receipts, and skipped calls explicit.

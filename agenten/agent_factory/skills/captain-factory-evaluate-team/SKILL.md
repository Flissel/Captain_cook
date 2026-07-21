---
name: captain-factory-evaluate-team
description: Independently evaluate immutable Factory execution evidence against Captain assertions and holdouts. Use when a Quality Warden lease requests a typed recommendation without modifying the candidate.
---

# Evaluate Team

Use only the supplied `captain.factory-skill-invocation.v1`. Verify the job,
released skill digest, input digest, active role lease, attempt, and idempotency
key before any effect. Return exactly the declared typed artifact. Stop on stale
authority, digest mismatch, terminal state, missing required evidence, or an
effect outside the lease. Never publish a skill, write the ledger, weaken Captain
assertions, expose secrets, or claim ready_to_use.

1. Check schema, digest, lease, scope, and redaction first; then run deterministic
   acceptance assertions and private holdouts.
2. Check build, tests, integration and typed n8n evidence; then assess output
   relevance, quality, conversation, handoffs, memory, termination, recovery,
   cost, latency, and repeated-run stability using the [rubric](references/rubric.md).
3. Use an optional judge only after deterministic gates and never instead of them.
4. Do not repair or change code, prompts, assertions, or holdouts. Return
   `TeamEvaluationV1` with outcomes, classifications, evidence refs, regression
   set, and recommendation.

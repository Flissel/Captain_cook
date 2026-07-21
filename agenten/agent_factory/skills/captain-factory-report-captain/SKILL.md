---
name: captain-factory-report-captain
description: Submit complete redacted Factory evidence and one lifecycle recommendation to Captain. Use when a Quality Warden lease must report an immutable candidate without making the release decision.
---

# Report Captain

Use only the supplied `captain.factory-skill-invocation.v1`. Verify the job,
released skill digest, input digest, active role lease, attempt, and idempotency
key before any effect. Return exactly the declared typed artifact. Stop on stale
authority, digest mismatch, terminal state, missing required evidence, or an
effect outside the lease. Never publish a skill, write the ledger, weaken Captain
assertions, expose secrets, or claim ready_to_use.

1. Bind candidate, execution, evaluation, cost, tool-gap, lease, and invocation
   evidence digests. Redact secret-bearing data.
2. Choose exactly one recommendation from [the approved set](references/recommendations.md)
   and explain it with assertion and gap identifiers.
3. Return `FactoryFeedbackV1`. Captain decides and independently recomputes the
   lifecycle decision; this feedback is never a Gateway release decision.

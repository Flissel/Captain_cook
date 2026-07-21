---
name: captain-factory-improve-team
description: Produce a bounded child revision of a failed Factory candidate. Use only after Captain emits IMPROVEMENT_REQUESTED with evidence-linked failures and a new Tool Integrator lease.
---

# Improve Team

Use only the supplied `captain.factory-skill-invocation.v1`. Verify the job,
released skill digest, input digest, active role lease, attempt, and idempotency
key before any effect. Return exactly the declared typed artifact. Stop on stale
authority, digest mismatch, terminal state, missing required evidence, or an
effect outside the lease. Never publish a skill, write the ledger, weaken Captain
assertions, expose secrets, or claim ready_to_use.

1. Start only after `IMPROVEMENT_REQUESTED`; bind each failure to exact assertion
   and evidence references using the [repair assignment](templates/repair-assignment.md).
2. Change only evidence-implicated code, prompts, context, tools, model clients,
   memory, conversation/handoffs, termination, n8n workflow/nodes, tests, or
   technical documentation.
3. Rerun every prior green assertion. Seal the result as a child candidate with
   precise change-to-evidence mapping, then return it to build, execution, and
   independent evaluation.
4. Return `CandidateRevisionV1`. Never promote; behavioral failure consumes the
   released attempt while infrastructure recovery resumes the same attempt.

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

1. Start only after `IMPROVEMENT_REQUESTED`. Validate the supplied
   `captain_improvement_seed` digest; it contains the exact failed assertion,
   benchmark, evidence, prior-candidate, and prior-green bindings described by
   the [repair assignment](templates/repair-assignment.md).
2. Change only evidence-implicated code, prompts, context, tools, model clients,
   memory, conversation/handoffs, termination, n8n workflow/nodes, tests, or
   technical documentation. Only the Tool Integrator may use a separate
   short-lived `n8n-builder` profile and `n8n-mcp` lease when the compiled
   specification declares `integration_intent=n8n`. Limit changes to an isolated
   draft; never allow activation, production adoption, service administration,
   or volume management.
3. Select only the smallest evidence-implicated `changed_components` enum set.
   Do not invent artifact references, lifecycle identity, timestamps, candidate
   IDs, Codex sessions, or test results. Captain materializes `CandidateRevisionV1`
   from the digest-bound response and later Codex/test evidence.
4. Return exactly one `hermes.factory-improvement-attestation.v1` JSON object
   with the supplied invocation ID and seed digest, the selected unique
   `changed_components`, and `accepted=true`. Use no tools. Never promote;
   behavioral failure consumes the released attempt while authorized runtime
   recovery resumes the same attempt.

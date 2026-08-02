---
name: captain-factory-brief-codex
description: Validate Captain's bounded Codex build assignment for a validated Factory inventory and attest its digest. Use before a Tool Integrator delegates one dependency-ready work unit.
---

# Brief Codex

Use only the supplied `captain.factory-skill-invocation.v1`. Verify the job,
released skill digest, input digest, active role lease, attempt, and idempotency
key before any effect. Return exactly the declared typed artifact. Stop on stale
authority, digest mismatch, terminal state, missing required evidence, or an
effect outside the lease. Never publish a skill, write the ledger, weaken Captain
assertions, expose secrets, or claim ready_to_use.

1. Validate the supplied `captain_codex_brief_seed` and its SHA-256 against the
   invocation, the completed Attempt-1 discovery inventory, the V3 job, the
   released skill, the current Tool Integrator lease, and Captain's assertions.
   Do not reproduce the Captain-authored brief and do not call tools.
2. Confirm that the brief selects the smallest dependency-ready work node and
   binds its goal, measurable outcome, authorized workspace, architecture
   constraints, test IDs, tool policy, model/budget limits, and evidence.
   Use the [assignment template](templates/codex-assignment.md) only as a
   structural checklist; do not copy or regenerate the seed.
3. Confirm that the Captain-authored brief instructs Codex: For build work, load
   and follow `plan` and `test-driven-development`. Load `systematic-debugging`
   only after a diagnosed failure and only for its repair step. Load
   `requesting-code-review` before completion. Codex execution belongs to the
   separately leased seal step, never to this attestation step.
4. Only the Tool Integrator may use a separate short-lived `n8n-builder` profile
   and `n8n-mcp` lease when the compiled specification declares
   `integration_intent=n8n`. Limit the assignment to an isolated draft; never
   allow activation, production adoption, service administration, or volume
   management. Require the Captain-authored brief to delegate technical n8n
   construction to the pinned official `n8n-io/skills` source. Codex must load
   `using-n8n-skills-official`, then the routed lifecycle, node, agent, error,
   and credential skills, and use the official instance-level MCP sequence:
   SDK reference, exact node types, validation, create/update, and read-back
   verification. Custom Captain skills retain lease/evidence authority but do
   not replace upstream n8n build guidance.
5. Require the brief to demand code, tests, manifests, digests, and command
   evidence; candidate inputs and outputs remain subject to Captain's seal. Do
   not grant an approval bypass.
6. Return only `hermes.factory-codex-brief-attestation.v1` with the exact
   invocation ID, seed digest, and `accepted=true`. This digest-bound
   attestation is evidence that Hermes applied the skill; Captain remains the
   sole author of `CodexBuildBriefV1`.

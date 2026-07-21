---
name: captain-factory-discover
description: Inspect a Captain-released Factory job before architecture or generation. Use when an Agent Architect lease requests a typed inventory of reusable code, contracts, tests, tools, or declared integrations.
---

# Discover Factory Inputs

Use only the supplied `captain.factory-skill-invocation.v1`. Verify the job,
released skill digest, input digest, active role lease, attempt, and idempotency
key before any effect. Return exactly the declared typed artifact. Stop on stale
authority, digest mismatch, terminal state, missing required evidence, or an
effect outside the lease. Never publish a skill, write the ledger, weaken Captain
assertions, expose secrets, or claim ready_to_use.

1. Inspect the released workspace semantically with `rg`, imports, entrypoints,
   tests, schemas, and tool search. Use builtin `codebase-inspection` only for
   metrics, never as semantic inspection.
2. Read Context7 AutoGen documentation when the installed version affects a
   decision. Read n8n documentation only for a declared integration.
3. Map reusable components, assertion coverage, uncertainty, conflicts, and
   missing capabilities. Do not change code or create artifacts outside the
   declared output.
4. Return `CodebaseInventoryV1` following the [output schema](references/output-schema.md),
   with content-addressed observations and no source bodies, secrets, absolute
   user paths, or raw terminal output.

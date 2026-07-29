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

1. When `captain_discovery_seed` is present, validate it without calling tools;
   Captain has already content-addressed the allowlisted sources, tests,
   schemas, entrypoint, revision, and AutoGen pin. Return only the exact
   `hermes.factory-discovery-attestation.v1` from
   `captain_required_output_bindings`; never reproduce or alter the seed.
   Otherwise,
   work only in the current Captain workspace. Never inspect prior sessions,
   memories, or the Hermes implementation checkout. Use at most eight tool
   calls total: one bounded `rg`/file search, one revision lookup, and at most
   four focused file reads before returning the typed inventory. Do not call
   `session_search` or `skill_view`; this released skill is already injected.
   Use builtin `codebase-inspection` only for metrics, never as semantic
   inspection.
2. Read Context7 AutoGen documentation when the installed version affects a
   decision. Read n8n documentation only for a declared integration.
3. Map reusable components, assertion coverage, uncertainty, conflicts, and
   missing capabilities. Do not change code or create artifacts outside the
   declared output.
4. Treat `captain_output_json_schema` in the invocation prompt as the complete
   wire contract. Copy every `captain_required_output_bindings` value exactly.
   For a seeded discovery this is the small digest-bound attestation; Captain
   materializes the already validated inventory after checking that exact
   attestation. For an unseeded discovery, never regenerate the invocation
   schema or idempotency key, always include `inspected_revision` and
   `autogen_version`, and return `CodebaseInventoryV1` following the
   [output schema](references/output-schema.md),
   with content-addressed observations and no source bodies, secrets, absolute
   user paths, or raw terminal output.

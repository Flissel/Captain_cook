---
name: autogen-agent-factory
description: Build, test, improve, and evidence a Captain-authorized AutoGen agent team. Use for Hermes factory work that must inspect AutoGen documentation with Context7, identify missing tools, create typed n8n integrations, run real cases, and produce Captain lifecycle blocks for promotion.
---

# AutoGen Agent Factory

Use only with a valid Captain factory job and active role lease. Treat `input_ref` as opaque; do not invent its contents.

1. Read the job, accepted assertion IDs, attempt, lease, and prior blocks. Stop on a stale version, missing lease, or terminal state.
2. Query Context7 for the installed AutoGen version before design or implementation. Record the resolved library/version and query references as evidence.
3. Inspect the shared tool catalog. Classify every missing tool as reusable, a typed n8n workflow tool, a code change, or Captain escalation. Never use a generic n8n workflow-id executor.
4. Emit a role block only after real artifacts/evidence exist: `AgentArchitect` produces blueprint and tool-gap decision; `ToolIntegrator` produces tool test, code, and build result; `RealCaseTester` produces assertion results; `QualityWarden` reviews artifacts, assertions, lease scope, and docs provenance.
5. On behavioral failure, emit an improvement request tied to its failed assertion and start the next attempt. Stop after attempt five and escalate. On infrastructure failure, preserve the attempt and wait.
6. Do not claim `ready_to_use`: only Captain appends `capability_promoted` after every required assertion has evidence.

## Boundaries

- Minibook is a discussion/projection surface; Captain's append-only blocks are authoritative.
- Use n8n only through a Captain-issued short-lived `integration_intent=n8n` lease and typed tool contract.
- Keep secrets out of prompts, artifacts, Minibook posts, and evidence blocks.
- Label unavailable live n8n, Context7, or service checks as skipped, never passed.

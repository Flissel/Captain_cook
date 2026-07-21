---
name: autogen-agent-factory
description: Build, test, improve, and evidence a Captain-authorized AutoGen agent team. Use for Hermes factory work that must inspect AutoGen documentation with Context7, identify missing tools, create typed n8n integrations, run real cases, and produce Captain lifecycle blocks for promotion.
---

# AutoGen Agent Factory

Use only with a valid Captain factory job and active role lease. Treat `input_ref` as opaque; do not invent its contents.

1. Read the job, accepted assertion IDs, attempt, lease, and prior blocks. Verify the released skill and job digests before any effect. Stop on a stale version, missing lease, digest mismatch, or terminal state.
2. Retrieve AutoGen documentation through Context7 for the installed version before architecture or code decisions. Record the official library identifier, installed/retrieved version, query digest, source references, retrieval time, and content digest as evidence.
3. Retrieve n8n documentation and catalog evidence only for integrations declared by the compiled specification. Resolve each tool in this order: released typed tool, documented native n8n node, approved n8n MCP operation, typed HTTP workflow, tested self-built local adapter, then `TODO_TOOL.v1`. Never use a generic n8n workflow-id executor.
4. Before build validation, produce one `captain.factory-candidate.v1` manifest and a ZIP of the generated source. Bind the team manifest, every n8n workflow, and every input/output schema by safe relative path plus SHA-256. Each typed tool's schema references must resolve to those bindings.
5. Assign each dependency-ready work node to Codex using only the authorized workspace and a bounded command. Require code, tests, manifests, and real receipts rather than prose. Preserve prior green assertions on every retry.
6. Run the matching leased validation action with `python -m agenten.agent_factory.evaluation_cli`. Its JSON output is the only acceptable build, real-case, or quality block. The evaluator uses a new temporary workspace, strips inherited secrets, checks digests, compiles Python, runs the build command, and requires the real case to return the Captain trace ID plus exactly the accepted assertion IDs.
7. Emit a role block only after real artifacts/evidence exist: `AgentArchitect` produces blueprint and tool-gap decision; `ToolIntegrator` produces tool test, code, and build result; `RealCaseTester` produces assertion results; `QualityWarden` reviews artifacts, assertions, lease scope, and docs provenance.
8. Retain an immutable private candidate only after the assigned capability succeeds. Failed, blocked, or cancelled work retains evaluation evidence but no candidate.
9. On behavioral failure, emit an improvement request tied to its failed assertion and start the next attempt. Stop after attempt five and escalate. On infrastructure failure, preserve the attempt and wait.
10. Never publish a skill and never claim `ready_to_use`: only Captain validates, publishes, and appends `capability_promoted` after every required assertion has evidence.

## Boundaries

- Minibook is a discussion/projection surface; Captain's append-only blocks are authoritative.
- Use n8n only through a Captain-issued short-lived `integration_intent=n8n` lease and typed tool contract.
- Keep secrets out of prompts, artifacts, Minibook posts, and evidence blocks.
- Label unavailable live n8n, Context7, or service checks as skipped, never passed.

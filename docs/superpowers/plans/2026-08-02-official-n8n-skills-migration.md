# Official n8n skills migration

## Goal

Use the official `n8n-io/skills` package for technical n8n workflow construction
while preserving Captain as the only lifecycle, lease, cost, evidence, retry,
and promotion authority.

## Reviewed release

- Repository: `https://github.com/n8n-io/skills`
- Commit: `046c330c9308bbfc54ceab1adbe3d8fc6bebc8fa`
- Plugin: `n8n-skills@n8n-io`
- Plugin version: `1.1.0`
- Instance MCP: `n8n` at `http://localhost:5679/mcp-server/http`
- Token source: `N8N_MCP_TOKEN`

## Work portions

- [x] Add a strict Captain-owned lock for the reviewed official release.
- [x] Add an idempotent, fail-closed Codex plugin and MCP configurator.
- [x] Attach the official n8n build protocol only to assignments that declare n8n.
- [x] Migrate Hermes discovery, brief, improvement, and AutoGen factory skills.
- [x] Preserve the Captain lease and promotion boundary in architecture docs.
- [x] Add RED/GREEN acceptance coverage for lock, prompt, installer, and skills.
- [x] Run the focused verification gate.
- [x] Install and verify the pinned plugin in Codex and its skill directory in Hermes.
- [x] Run the final architecture and submission verification gates.

## Technical workflow sequence

1. Load `using-n8n-skills-official`.
2. Inspect the SDK reference and available node types through the approved MCP.
3. Build from official lifecycle, node, agent, error, and credential guidance.
4. Validate before writing.
5. Create or update the workflow, then read it back for evidence.
6. Return opaque evidence to Captain; never self-promote.

## Non-claims

- Installing the skills does not prove a workflow is production-ready.
- MCP reachability does not authorize writes outside a Captain lease.
- Official skill usage does not replace the business benchmark or live evidence.
- This migration does not change or delete existing n8n workflows or volumes.

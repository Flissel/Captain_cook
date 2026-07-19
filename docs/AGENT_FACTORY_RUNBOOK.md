# Agent Factory runbook

Use this runbook to execute the Captain-owned Agent Factory from the canonical
[`input.md`](../input.md). Captain remains the sole lifecycle and release
authority; Hermes, Minibook, n8n, and Codex are constrained workers.

## Required local state

Keep all secrets outside the repository. Before a live run, verify only their
presence:

```powershell
$env:TEST_MARIADB_DSN -ne $null
$env:N8N_API_KEY -ne $null
$env:N8N_MCP_TOKEN -ne $null
codex mcp get n8n-mcp
```

The n8n MCP endpoint must answer a non-destructive workflow-list call before a
factory job receives an `integration_intent=n8n` lease. See
[MCP setup](MCP_SETUP.md) for the user-level registration; do not commit its
token or modify VibeMind n8n volumes.

## Offline contract gate

Run this before requesting a live job:

```powershell
python -m pytest tests/agent_factory tests/agent_runtime/test_capabilities.py tests/gateway/test_factory_repository.py -q --no-cov
python scripts/verify_submission.py
```

This verifies canonical input parsing, Captain lease scope, typed n8n bindings,
gateway persistence adapters, lifecycle transitions, and the E2E release rule.
It is not evidence that a live LLM, n8n, Minibook, Docker, or Hermes run
occurred.

## Live execution sequence

1. Seal `input.md` as the content-addressed `artifact://factory-input/...`
   reference and create one factory job with its trace/correlation ID.
2. Persist the job and Captain's next role lease in the gateway.
3. Dispatch the leased Hermes role. It returns exactly one typed evidence block;
   Captain validates and appends it.
4. After tool-candidate evidence, materialize the sealed input and submit it to
   Minibook's existing `autogen_swarm.py --input-file` pipeline.
5. Bind every n8n workflow to a registered typed tool name. The agent call may
   contain a tool name, case ID, correlation ID, and typed payload—never a
   workflow ID.
6. Execute isolated static/build/real-case validation. Repeat behavioral repair
   no more than five times; preserve an infrastructure failure without charging
   an iteration.
7. Record one intentionally failing recovery scenario, then three consecutive
   successful normal E2E runs. Captain evaluates the release gate and only then
   appends `capability_promoted`.

## Expected projections

Minibook receives a redacted registry projection only after Captain records a
successful `capability_promoted` block. It receives neither leases, secrets,
nor raw evidence. Failed or incomplete lifecycle blocks remain authoritative in
Captain's gateway only.

## Troubleshooting and escalation

| Missing state | Safe offline alternative | Required decision |
| --- | --- | --- |
| `TEST_MARIADB_DSN` | Run the offline contract suite | Provide an isolated MariaDB DSN for restart/API proof. |
| n8n API key or unreachable MCP | Validate typed deployment contracts locally | Restore VibeMind n8n reachability and provide user-level credentials. |
| Hermes profile/model unavailable | Validate CLI request/evidence contracts | Configure the Hermes profile without placing provider secrets in this repo. |
| Missing input artifact | Do not start Forge | Restore or replace canonical `input.md` explicitly. |

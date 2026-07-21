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

## Hermes factory skills

Captain's repository remains the source of truth for the six workflow skills.
From the repository root, register that exact directory and create the operator
bundle:

```powershell
$repositoryRoot = (Resolve-Path .).Path
pwsh -NoProfile -File scripts/configure-hermes-factory-skills.ps1 `
  -RepositoryRoot $repositoryRoot
```

The command verifies the pinned directory digests, refuses missing, disabled,
or shadowed skills, and probes whether the installed Hermes CLI safely
round-trips an external-directory array before changing user configuration. If
that probe fails, upgrade Hermes and retry; the command exits before changing
the configured `HERMES_HOME`.

Verify the six enabled skills and the slash bundle:

```powershell
hermes skills list --enabled-only
hermes bundles show captain-agent-factory-loop
```

Rollback removes only this repository path and this bundle. It preserves unrelated external directories:

```powershell
pwsh -NoProfile -File scripts/configure-hermes-factory-skills.ps1 `
  -RepositoryRoot $repositoryRoot -Remove
```

The rollback does not reset builtin skills or delete other external skill
directories. It does not open or print `.env`.

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
6. Have Hermes create a `captain.factory-candidate.v1` manifest plus a ZIP of
   the generated source. Its content-addressed bindings must cover the team
   manifest, every n8n workflow, and every typed tool input/output schema.
   Run one matching validation lease for each lifecycle phase:

   ```powershell
   python -m agenten.agent_factory.evaluation_cli `
     --job <captain-job.json> --lease <captain-active-lease.json> `
     --candidate <sealed-candidate.json> --source-archive <generated-source.zip> `
     --action dispatch_build_validator --evidence-root artifacts/agent-factory/evidence
   ```

   Repeat with `dispatch_real_case_tester` and
   `dispatch_quality_warden`, each time using the active lease for that exact
   role. Append the JSON block returned by the CLI unchanged through the
   Captain gateway. The evaluator verifies all digests, compiles the extracted
   code, executes it in a new temporary workspace with provider/database/n8n
   secrets removed, and requires an exact trace ID and assertion set. It is
   still local isolated evidence; it does not claim a live n8n or LLM call.
   Repeat behavioral repair no more than five times; preserve an infrastructure
   failure without charging an iteration.
7. Record one intentionally failing recovery scenario, then three consecutive
   successful normal E2E runs. Captain evaluates the release gate and only then
   appends `capability_promoted`.

## Recording evidence handoff

The live integration owner exports a redacted
`captain.live-demo-evidence.v1` object outside the repository. Every stage and
all four run records carry the same UUID correlation ID and only opaque
`artifact://` references. Exclude tokens, authorization, raw provider data,
prompts, private holdouts, and host-local paths.

Runtime and n8n outcomes are `succeeded`, the Gateway decision is `accepted`,
and Minibook is `read-back`. Recovery records the expected failure and a
`recovered` outcome. Normal runs are distinct, ordered 1–3, and successful.

```powershell
$env:CAPTAIN_LIVE_EVIDENCE_INPUT = '<absolute path to redacted live export>'
python -m pytest -q --no-cov -m live tests/live/test_live_evidence_recording.py -rs
python -m docs.live_evidence_reporter `
  --input $env:CAPTAIN_LIVE_EVIDENCE_INPUT `
  --output artifacts/live-demo-a2-report.json
```

Absent input skips; configured but unreadable or rejected input fails. Never
replace this with a fixture or claim the reporter itself called live services.

## Six-skill paid live gate

Run the six-skill live gate only after deterministic and database-resetting
tests. The wrapper requires an explicit positive USD ceiling and an approved
model. It checks the exact `captain_test` database, the running Docker test
service, the six enabled Hermes skills and bundle, Codex authentication, and a
runtime preflight without printing command output or secret values. It starts
only the marked six-skill live test:

```powershell
pwsh -NoProfile -File scripts/run-hermes-factory-live-gate.ps1 `
  -Mode demo -MaxCostUsd 5.00 -Model $env:CAPTAIN_FACTORY_MODEL
```

The wrapper loads only its explicit variable allowlist from local `.env` and
`.env.captain-n8n` files. Existing process variables always win; dedicated
`CAPTAIN_N8N_*` values then come from `.env.captain-n8n`, with root `.env` as
the fallback. File contents and loaded values are never printed.

Demo mode runs exactly one paid case and may emit `demo_ready` only. After its
evidence is reviewed, release mode requires a controlled recovery followed by
three distinct successful provider traces. A declared integration additionally
uses `-WithN8n`; this requires the Captain n8n URL, API-key reference, and MCP
lease-token reference to be present in the process environment. The wrapper
does not start, stop, adopt, or modify the VibeMind-owned n8n deployment.

The final report is written below the operating-system temporary directory, in
a new run-specific directory, as `sha256-<digest>.json`. It is never written to
tracked `artifacts/`. Before parsing either preflight or final report, the
wrapper rejects secret-like keys (including access tokens, raw prompts,
private material, and paths), Bearer material, and absolute host paths. It then
recomputes the final digest and rejects an incorrect mode or terminal status.
It prints the report digest but not the report body or host path.

### Runtime merge contract

The runtime portion must expose the module
`agenten.agent_factory.factory_live_entrypoint` with both interfaces below.
This operations branch intentionally does not implement the composition root.

The wrapper invokes the CLI preflight before it marks prerequisites confirmed:

```text
python -m agenten.agent_factory.factory_live_entrypoint preflight
  --mode demo|release
  --max-cost-usd <positive decimal with cents>
  --model <approved model id>
  --repository-root <assigned repository root>
  --report-directory <external run directory>
  --output <external preflight.json>
  [--with-n8n]
```

The preflight output schema is
`captain.hermes-six-skill-factory-preflight.v1`. It must set
`prerequisites_confirmed`, `services_verified`, `codex_authenticated`, and
`skills_verified` to `true`, bind `database_name=captain_test`, and contain
exactly the six released skill names mapped to their verified lowercase
SHA-256 digests. The preflight must also prove Gateway/service reachability and
configuration required by the composition-neutral Factory live runner. It may
write a redacted diagnostic to the external output path, but must not print
provider, database, Gateway, Hermes, Codex, or n8n credentials.

After successful preflight, the wrapper sets these process-only inputs:

```text
CAPTAIN_FACTORY_PREREQUISITES_CONFIRMED=1
CAPTAIN_FACTORY_GATE_MODE=demo|release
CAPTAIN_FACTORY_MAX_COST_USD=<decimal>
CAPTAIN_FACTORY_MODEL=<model id>
CAPTAIN_FACTORY_WITH_N8N=0|1
CAPTAIN_FACTORY_REPORT_DIRECTORY=<external run directory>
CAPTAIN_FACTORY_PREFLIGHT_PATH=<external preflight.json>
```

The marked live test imports and awaits
`run_factory_live_gate_from_environment()`. The function returns either a
mapping or a Pydantic model whose JSON dump equals the one persisted
`captain.hermes-six-skill-factory-live-report.v1` report. It must propagate
missing evidence or execution failure; after the wrapper confirms prerequisites
it must never call `pytest.skip`. The live test converts any runner exception
outside its original exception context to a generic traceback-free failure,
and the wrapper suppresses both pytest output streams; neither surface may
print provider errors or secrets. Provider traces contain distinct trace and
Codex session IDs plus exact decimal USD receipts. Release output additionally
contains recovery evidence. With n8n enabled, it also binds the workflow
digest, MCP call ID, and real execution ID. The report contains neither raw
prompts/holdouts nor secrets or absolute host paths. Codex session IDs are
distinct per provider trace, every USD cost is a plain JSON decimal string,
and an n8n workflow digest is exactly one lowercase SHA-256 value.

Release mode must re-read the Gateway projection before it emits the report.
Its exact `gateway_promotion` block contains `projection_status=ready_to_use`,
a complete canonical `FactoryReleaseDecision` with `status=ready`, and a
complete canonical Captain `FactoryEvidenceBlock` for the succeeded
`capability_promoted` phase. Both contracts must bind to the report's job and
correlation, while the promotion block also binds subject version and attempt
and carries nonempty evidence references. The release decision's evaluation ID
and reference are mandatory, and the exact evaluation reference must occur in
the promotion block's artifact references. A terminal-status string alone is
not release evidence.

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

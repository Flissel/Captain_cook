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
codex mcp get n8n --json
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

## Official n8n build skills

Install the official `n8n-io/skills` plugin at Captain's reviewed commit pin:

```powershell
pwsh -NoProfile -File scripts/configure-official-n8n-skills.ps1
```

The installer adds `n8n-skills@n8n-io`, verifies the exact marketplace commit
and plugin version, registers that pinned skill directory with Hermes, and
validates the existing instance-level MCP registration without exposing its
token. It fails closed if `n8n` points anywhere except Captain's approved local
endpoint or uses a token source other than `N8N_MCP_TOKEN`. If the installed
Hermes CLI cannot round-trip multiple external skill directories, the installer
refuses to mutate the user configuration.

Verify the installation, then restart Codex so the newly installed skills and
hooks are loaded:

```powershell
codex plugin list
codex mcp get n8n --json
hermes skills inspect using-n8n-skills-official
```

For declared n8n integrations, Hermes and Codex begin with
`using-n8n-skills-official` and use the official lifecycle, node configuration,
agent, debugging, and credential skills. They use the instance-level MCP to
inspect node types, validate the workflow, create or update it, and read it back.
Captain's lease, budget, evidence, retry, and promotion rules remain authoritative.

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

## Business benchmark gate

Select exactly one approved profile and suite version per candidate attempt:

- Claims: `insurance_claims_resolution_swarm`, suite version 1.
- Renewal: `customer_renewal_orchestration_team`, suite version 1.

Captain also fixes the candidate/baseline model version, redaction and baseline
policy versions, per-case cost and latency ceilings, and the job-wide maximum
cost. Each private suite contains 15 runtime-loaded anonymized cases with
exactly three ordinary, boundary, incomplete, contradictory, and mandatory
escalation cases. Do not copy case bodies into logs, evidence exports, prompts,
Minibook, or source control.

Run the deterministic complete-chain gate first:

```powershell
python -m pytest -q --no-cov tests/integration/test_business_benchmark_factory.py
```

The lifecycle order is private suite, paired receipts, independent scoring,
private summary persistence, Gateway summary persistence, team evaluation,
feedback, quality review, release validation, Captain promotion, then Minibook
projection. A summary must be resolvable by its exact artifact digest before an
evaluation is accepted. Captain/Gateway is the sole `ready_to_use` authority.

Provider-backed runs are explicitly opt in and enforce the configured maximum
cost:

```powershell
pwsh -NoProfile -File scripts/run-business-benchmark-live.ps1 -Profile claims
pwsh -NoProfile -File scripts/run-business-benchmark-live.ps1 -Profile renewal
```

Live evidence is written only below
`.captain-cook/evidence/business-benchmarks/`. Private suites and case/run
receipts remain in the configured Captain-private benchmark store. Redacted
summaries and workflow evaluations are append-only Gateway records. Minibook
receives only aggregate disposition/reason codes, correctness/completion basis
points, cost/latency ratios, unsafe-tool/missed-handoff counters, the summary
digest, and the same correlation ID.

Failure reason codes include `missing_receipt`, `wrong_decision`,
`missing_rationale`, `unsafe_tool_intent`, `mandatory_handoff_missed`,
`below_minimum_correctness`, `below_baseline_correctness`,
`below_baseline_completion`, `cost_ratio_exceeded`,
`latency_ratio_exceeded`, and the two zero-baseline ratio failures. Missing
infrastructure/evidence remains blocked. Behavioral failure creates bounded
improvement feedback and a new candidate attempt; it never creates promotion.

For restart/recovery, rerun the same profile command with the same Captain job,
attempt, suite, candidate, and policy bindings. The replay store resumes the
durably prepared/fenced effect and rejects stale writers. If recovery is
uncertain, preserve the evidence and stop; do not start a duplicate provider
effect. After behavioral improvement, use the next Captain-authorized attempt
(maximum five). Useful checks are:

```powershell
python -m pytest -q --no-cov tests/agent_factory/test_business_benchmark_replay.py
python -m pytest -q --no-cov tests/gateway/test_factory_repository.py
python -m pytest -q --no-cov tests/gateway/test_registry_feed.py
```

These synthetic/anonymized suites validate release behavior and regression
resistance. They do not prove regulated-domain accuracy, legal compliance, or
production performance; those require separately governed real-domain
validation and live evidence.

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

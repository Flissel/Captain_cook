# Live Demo Recovery and Start Design

## Goal

Restore the executable Captain capability live-demo path on the current
integration baseline and provide one idempotent PowerShell entrypoint that
starts, validates, and optionally runs the demo without silently changing
credentials or incurring provider cost.

## Current problem

The active tree retains the live-demo runbook but no longer contains the
capability runner, production adapter bundle, or related Gateway composition
that the runbook invokes. The runtime entrypoint therefore fails closed after
settings validation. In addition, the Minibook helper assumes port `8080`
although the application currently listens on its configured port `3456`, and
the stored Minibook demo credential does not authenticate against that
instance.

Captain n8n REST and MCP, the MCP broker, n8n Postgres, MariaDB `captain_test`,
Mailpit, the Gateway, and Minibook can otherwise run locally. The repair must
preserve those working services and must not adopt or mutate VibeMind-owned
containers or volumes.

## Chosen approach

Selectively port the required live components from the historical commits onto
the current architecture. Do not merge an old live branch wholesale: those
branches are hundreds of commits behind the current baseline and contain broad
contract and storage changes that would reintroduce obsolete behavior.

The implementation has three bounded layers:

1. **Service lifecycle:** make Minibook health use the configured local port,
   preserve fail-closed credential handling, and expose one start/status entry.
2. **Production runtime composition:** restore typed Hermes, Codex, artifact,
   n8n, Gateway, and Minibook adapters behind the current runtime and Factory
   contracts.
3. **Demo orchestration:** restore the three-input capability runner, recovery,
   restart/resume, evidence validation, and optional recording behind explicit
   provider-cost authorization.

## User-facing command

Create `scripts/start-live-demo.ps1` as the canonical entrypoint.

The default invocation is infrastructure-only and must not call a paid model:

```powershell
pwsh -NoProfile -File scripts/start-live-demo.ps1
```

It performs configuration validation, starts only Captain-owned local services,
waits on authenticated health checks, and prints a redacted service summary.

The provider-backed demo requires all three explicit arguments:

```powershell
pwsh -NoProfile -File scripts/start-live-demo.ps1 `
  -RunDemo `
  -ConfirmProviderCost `
  -MaxCostUsd 6.00
```

`-RunDemo` without `-ConfirmProviderCost`, or without a positive finite
`-MaxCostUsd`, fails before any provider call. The limit is the total demo
ceiling; the orchestrator derives per-input ceilings without exceeding it.

## Service lifecycle

The entrypoint composes existing focused helpers rather than duplicating their
implementation:

- `scripts/live-demo-services.ps1` owns Captain n8n, MariaDB `captain_test`,
  Gateway, Runtime, Mailpit, and service evidence.
- `scripts/minibook-demo.ps1` owns the local Minibook process identity and
  health check.
- `scripts/run-capability-live-demo.ps1` owns the three-input execution and
  restart/resume evidence.

Minibook derives its base URL from its existing configuration or the configured
`MINIBOOK_BACKEND_URL`. It must not contain a second hard-coded port. A running
Minibook on the configured endpoint is reused only after health validation and
managed-process identity validation where applicable.

The start command is idempotent. A second invocation must reuse exact healthy
managed services and must reject an unrelated process occupying a required
port. It never deletes a container, volume, database, workflow, or credential.

## Credential policy

The command validates existing gitignored credentials but does not repair,
rotate, create, or replace them automatically.

In particular:

- n8n REST and MCP credentials must authenticate before n8n is reported ready.
- the Minibook demo API key must resolve the expected `captain-demo-service`
  identity;
- the projection key must be present and remain private;
- Gateway and Runtime bearer values must be non-empty;
- no key or provider response body may appear in console or evidence output.

An invalid Minibook key stops with a redacted command describing the explicit
recovery action. Credential recovery remains a separate operator decision.

## Production composition

The runtime entrypoint must stop raising an unconditional configuration error.
After settings validation it loads digest-bound adapter manifests and composes
the existing Gateway-backed runtime service with explicit production ports:

- Hermes planner port;
- Codex execution/resume/cancellation port;
- content-addressed artifact port;
- Factory production adapter bundle;
- typed n8n integration adapter when an `integration_intent=n8n` lease exists;
- Gateway release/execution authority;
- Minibook projection/read-back adapter.

Missing modules, manifests, digest mismatches, unsupported schemas, missing
leases, or unavailable endpoints fail before an external effect. Historical
implementations may be used as behavioral references, but their contracts must
be adapted to the current Gateway store, business benchmark, retry, and
projection models.

## Demo data flow

The provider-backed path uses exactly three canonical `TO_BE_BUILT.md` inputs
with distinct AutoGen conversation patterns:

```text
TO_BE_BUILT.md
  -> Captain decomposition and capability lookup
  -> Hermes released skill workflow
  -> Codex build, test, and seal
  -> optional leased n8n integration
  -> deterministic plus provider-backed evaluation
  -> Gateway release decision and execution
  -> Minibook projection and read-back acknowledgement
  -> Captain ready_to_use evidence
```

The first correlation includes one controlled recovery. Each newly created
capability includes three distinct normal E2E traces. Restart/resume reuses the
first correlation and preserves capability, command, result, and projection
identities.

## Evidence and recording

All mutable demo state and redacted evidence remains below `.captain-cook/`.
The final summary records only stable IDs, hashes, statuses, durations, model
identity, and booked cost. It excludes prompts, provider bodies, credentials,
private benchmark cases, and absolute user paths.

Recording remains opt-in and captures only a named, dedicated window. Missing
FFmpeg or an ambiguous window title stops before capture without changing the
successful demo evidence.

## Error handling

Every stage fails closed and identifies the first unmet prerequisite:

- invalid Compose configuration: start nothing;
- unavailable or unauthenticated dependency: do not report it ready;
- stale PID or unrelated port owner: refuse reuse or termination;
- missing adapter manifest or digest mismatch: do not start Runtime;
- absent cost confirmation: perform no provider call;
- failed Factory action or unresolved required tool gap: do not promote;
- missing Minibook read-back: do not advance projection evidence;
- failed restart identity check: do not claim resume success.

The entrypoint returns a non-zero exit code and writes no green summary for a
partial run.

## Testing strategy

Implementation follows test-first RED/GREEN cycles.

Required automated coverage:

1. Minibook uses the configured endpoint and rejects an incorrect endpoint.
2. Invalid Minibook credentials fail without modifying `.env` or the database.
3. Default start performs no provider invocation.
4. Provider execution requires explicit confirmation and a valid total budget.
5. Repeated start reuses exact managed process identities.
6. Missing or digest-mismatched adapter manifests stop Runtime startup.
7. The three selected inputs have distinct conversation patterns.
8. Recovery, three normal traces, promotion, execution, projection, and
   restart/resume identities are all required by the final evidence gate.
9. Architecture/import-boundary tests remain green.
10. The complete non-live regression suite passes before live execution.

The final verification sequence is:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not live"
.\.venv\Scripts\python.exe scripts/verify_submission.py
.\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config gateway
pwsh -NoProfile -File scripts/start-live-demo.ps1
pwsh -NoProfile -File scripts/start-live-demo.ps1 -RunDemo -ConfirmProviderCost -MaxCostUsd 6.00
```

The paid command is executed only after the deterministic and infrastructure
gates pass and the user has authorized that run.

## Non-goals

- No VibeMind n8n adoption, migration, restart, credential rotation, or volume
  operation.
- No automatic Minibook credential repair.
- No production deployment to the Mini-PC.
- No broad merge of historical live branches.
- No claim of regulated-domain accuracy from synthetic benchmarks.

## Acceptance criteria

The work is complete when:

- one default command starts and authenticates every local prerequisite without
  provider calls;
- a second invocation is idempotent;
- Runtime is composed from verified production adapters instead of the current
  unconditional failure;
- the cost-authorized command processes three distinct inputs and produces the
  required recovery, E2E, promotion, execution, projection, and restart/resume
  evidence;
- all evidence is redacted and gitignored;
- the regression, architecture, submission, and compile gates are green.

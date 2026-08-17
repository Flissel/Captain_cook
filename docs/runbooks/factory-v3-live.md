# Factory V3 live Hermes/Codex gate

## Current verified state

Status on 2026-08-17: **unmet preflight; no live build started**.

The frozen `sales_pipeline_brief` input digest is
`81e76f52fdc99f64aeea9e5008c00d6eba25972014ef7563d4fc0851cf85864f`.
Read-only preflight run `a139b6f8-8c8b-4f65-b598-5656d32481e0` returned these
sanitized unmet checks:

- `live_opt_in`
- `gateway_reachability`
- `minibook_reachability`
- `released_skill_authority`
- `hermes_runtime`
- `codex_session`
- `credential_separation`
- `execution_bounds`
- `portal_prerequisites`
- `runtime_enforcement`

The load-bearing code blocker is `runtime_enforcement`: the present production
composition creates `PowerShellCodexRunner`. Its Codex workspace sandbox and
post-run evidence do not Captain-enforce the assignment's exact writable roots,
the exact two build argv values, and the provider-cost ceiling. It therefore is
not accepted as Task-10 proof. The hermetic restricted Task-9 engine is also not
a live substitute. The Gateway currently has no read-only, correlation-bound
active released-skill preflight, so local skill bytes or environment metadata
are not accepted as Captain authority.

No correlation ID, Hermes invocation, Codex session, Minibook receipt, package
digest, or Captain acceptance exists for Task 10 yet.

## Safe invocation

The gate accepts only the frozen fixture and creates a fresh local run directory:

```powershell
$env:CAPTAIN_FACTORY_V3_LIVE = '1'
pwsh -NoProfile -File scripts/run-factory-v3-live.ps1 `
  -Input tests/fixtures/agent_factory/sales_pipeline_brief.v3.json
```

The script runs preflight before composing any worker or provider port. Exit 3
means unmet preflight; exit 4 means durable Action Inbox input is pending; exit
5 means the runtime enforcement port is still unavailable. A nonzero exit is
not live success.

To resume an existing pending Action Inbox run, use its locally persisted
`preflight.json`; do not edit environment or application configuration around
the gate:

```powershell
pwsh -NoProfile -File scripts/run-factory-v3-live.ps1 `
  -Input tests/fixtures/agent_factory/sales_pipeline_brief.v3.json `
  -ResumeMetadata <run-preflight-json>
```

When a credential action is pending, output is limited to status, safe Inbox
URL, request ID, and checkpoint digest. Secret values are submitted only through
the authenticated credential boundary; downstream configuration receives an
opaque `credential://` reference.

## Required non-secret bindings

The preflight requires explicit opt-in, safe Gateway/Minibook health URLs,
distinct Captain/worker/creation credentials, a dedicated Hermes Python/module
root, an active released-skill name/digest/root verified through Captain
authority, the fixed Codex session launcher and Codex home, safe Portal or
Action Inbox metadata, UTC deadline, file/runtime/cost caps, and
`CAPTAIN_FACTORY_V3_NETWORK_ALLOWED=false`.

The fresh workspace, CAS, and state roots must exist, be distinct non-symlink
directories, and must not contain one another. The frozen assignment requires:

```text
python -m compileall src
python -m pytest
```

Only a future Captain-owned live runner that technically enforces those exact
argv values, the exact `src`/`tests`/`evidence` writable roots, network denial,
deadline/file limits, and cost cap can make `runtime_enforcement` pass. An
environment assertion, self-authored receipt, mock, deterministic adapter, or
legacy fallback cannot.

## Verification recorded on 2026-08-17

- Focused preflight/operator/runner tests: `6 passed`.
- Focused operator and Codex execution regressions: `114 passed`.
- Root Agent Factory/Gateway non-live gate: `1880 passed, 132 skipped`.
- Isolated Minibook gate: `259 passed, 1 skipped`.
- Portal: `47 passed`; lint and production build passed.
- Opt-in Task-10 live test: failed closed with the ten unmet checks above.

The skipped tests were service/MariaDB-gated and are not live evidence.

## Explicit non-claims

- No real Hermes released-skill invocation occurred.
- No authorized Codex session started.
- No live Minibook build, package, CAS readback, or Captain acceptance occurred.
- No OpenFang action, promotion, deployment, or `ready_to_use` event occurred.
- No service was started, reconfigured, or mutated by this preflight.
- No push or merge occurred.

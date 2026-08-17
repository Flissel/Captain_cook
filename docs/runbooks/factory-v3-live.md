# Factory V3 live Hermes/Codex gate

## Current verified state

Status on 2026-08-17: **unmet preflight; no live build started**.

The frozen `sales_pipeline_brief` input digest is
`81e76f52fdc99f64aeea9e5008c00d6eba25972014ef7563d4fc0851cf85864f`.
Historical read-only preflight run `a139b6f8-8c8b-4f65-b598-5656d32481e0`
returned the sanitized unmet checks below before the staged-preflight
hardening. No replacement live preflight was executed during that hardening:

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

To resume an existing pending Action Inbox run, use its canonical
`resume-metadata.json`; a preflight report is never resume authority. The file
contains only IDs, an opaque checkpoint reference, deadline, tenant, and
Captain digest/signature. Preflight authenticates to Gateway, rereads the
current action row, and requires exact equality before resuming:

```powershell
pwsh -NoProfile -File scripts/run-factory-v3-live.ps1 `
  -Input tests/fixtures/agent_factory/sales_pipeline_brief.v3.json `
  -ResumeMetadata <run-resume-metadata-json>
```

When a credential action is pending, output is limited to status, safe Inbox
URL, request ID, and checkpoint digest. Secret values are submitted only through
the authenticated credential boundary; downstream configuration receives an
opaque `credential://` reference.

## Required non-secret bindings

The preflight requires explicit opt-in, canonical Gateway/Minibook authority
origins (`CAPTAIN_FACTORY_V3_GATEWAY_ORIGIN` and
`CAPTAIN_FACTORY_V3_MINIBOOK_ORIGIN`),
distinct Captain/worker/creation credentials, a dedicated Hermes Python/module
root, an active released-skill name/digest/root verified through Captain
authority, the fixed Codex session launcher and Codex home, safe Portal or
Action Inbox metadata, UTC deadline, file/runtime/cost caps, and
`CAPTAIN_FACTORY_V3_NETWORK_ALLOWED=false`.

Health paths are fixed and derived from those origins. Caller-provided health
URLs are rejected. A successful HTTP status is insufficient: each service must
return the exact typed service identity, V3 version, capabilities, origin, and
readiness document. If any local gate (including opt-in and runtime
enforcement) is absent, no endpoint or released-skill authority call occurs.

The frozen fixture SHA-256 and the full topology/tool/build-authority manifest
are pinned. The Codex launcher content and required safety markers are pinned;
the Hermes runtime must be the clean initialized submodule at its pinned Git
identity, not an arbitrary Python executable or fabricated module tree.

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

Live success is not read from a scalar `evidence.json`. The live gate accepts
only a locator index, then independently reads exactly one typed Hermes receipt,
one Captain-sealed Codex receipt, one Minibook result, and one Captain
acceptance under the same preflight run and correlation. Hermes/Codex/result
receipts plus team manifest, source archive, and readback receipt must match
independent Minibook and Captain CAS reads. A recursive secret scanner rejects
secret-bearing keys and token-like values before any verified summary is
emitted.

## Verification recorded on 2026-08-17

- Focused hardened preflight/evidence/runner tests: `48 passed`.
- Root Agent Factory/Gateway non-live gate: `1921 passed, 132 skipped`.
- Isolated Minibook gate: `259 passed, 1 deselected`.
- Architecture/import/workstream gate: `18 passed`.
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

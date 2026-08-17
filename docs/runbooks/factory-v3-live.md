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

## Required signed authority binding

Before reading a credential or probing any executable/endpoint, preflight loads
only `.captain-cook/private/factory-v3-live/authority-binding.json`. The file
must be canonical JSON signed with the Captain Factory V3 Ed25519 authority;
the verification public key and key ID are pinned in the product. A missing,
self-signed, modified, symlinked, or structurally incomplete document stops the
gate locally.

The signed document is the authority for both exact service origins and health
identity/capabilities/audiences, all three executable digests and versions, the
Codex launcher digest/version, Hermes Git/runtime/module digests and version,
the active released-skill digest, and the authorized live-runner attestation
public key. Runtime URL variables cannot grant authority: when supplied, they
must only repeat the signed origins exactly. No private signing material belongs
in the repository or in the preflight report.

## Required non-secret bindings

The preflight requires explicit opt-in, the signed canonical Gateway/Minibook
authority origins,
distinct Captain/worker/creation credentials, a dedicated Hermes Python/module
root, an active released-skill name/digest/root verified through Captain
authority, the fixed Codex session launcher and Codex home, safe Portal or
Action Inbox metadata, UTC deadline, file/runtime/cost caps, and
`CAPTAIN_FACTORY_V3_NETWORK_ALLOWED=false`.

Health paths are fixed and derived from the signed origins. Caller-provided
health URLs are rejected. A successful HTTP status is insufficient: each
service must return the exact typed service identity, V3 version, capabilities,
credential audience, origin, and readiness document. If any local gate
(including signed authority, opt-in, and runtime enforcement) is absent, no
endpoint, token-bearing read, local executable probe, or released-skill
authority call occurs.

The frozen fixture SHA-256 and the full topology/tool/build-authority manifest
are pinned. Before endpoint reads, Codex, PowerShell, and Hermes Python must
match their signed whole-file SHA-256 values and return the exact signed
version/identity documents from bounded, noninteractive local probes with a
minimal environment. The Codex launcher content and required safety markers
are pinned. Hermes must additionally match the signed clean submodule Git
identity, bounded runtime-tree digest, module-file digest, distribution version,
and runtime capabilities. A renamed executable or fabricated Hermes tree fails
before it can run.

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

Live success is not read from a scalar `evidence.json`. The locator index must
carry the same signed-authority and active-skill digests as preflight. The gate
then requires exactly one Ed25519-signed `mode=live` runner/session attestation
bound to the preflight run, correlation, binaries, launcher, Hermes runtime,
workspace policy/snapshot, assignment, effect, and Codex session. Hermetic or
deterministic receipts cannot satisfy that contract.

The verifier independently reads the typed Hermes/Codex/Minibook/Captain
records and every Hermes evidence reference, Codex session/workspace/test
reference, Minibook build/real-case reference, package-manifest child, Captain
acceptance, and signed attestation through the public CAS. Authority records and
top-level package evidence must also agree byte-for-byte across the independent
Minibook and Captain CAS views. A recursive secret scanner rejects
secret-bearing keys and token-like values before any verified summary is
emitted.

## Verification recorded on 2026-08-17

- Focused hardened authority/preflight/evidence tests: `55 passed`.
- Root Agent Factory/Gateway non-live gate: `1930 passed, 132 skipped`.
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

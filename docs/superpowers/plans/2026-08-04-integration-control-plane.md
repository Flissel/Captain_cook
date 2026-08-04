# Production-near integration control plane

## Goal

Make external integrations usable without exposing credentials to Captain,
Hermes, Codex, AutoGen, Minibook, prompts, artifacts, or workflow JSON. Captain
derives an exact setup requirement from the released integration inventory;
the user creates the named credential type in the n8n UI; Captain binds only
credential metadata and issues execution authority only after a safe probe.

## Fixed boundaries

- Captain owns requirements, readiness, leases, validation, audit, and release.
- n8n owns encrypted credential values and provider-side authentication.
- Credential creation and rotation stay in the n8n UI.
- Agents receive typed tool capabilities, never secrets or credential values.
- Minibook receives redacted readiness projections only.
- Required unresolved credentials block execution and promotion fail-closed.
- VibeMind n8n resources remain untouched.

## Work portions

- [x] Add frozen setup requirement, sanitized credential metadata, verification
  receipt, connection, and plan contracts.
- [x] Add deterministic matching: zero credentials is missing, one exact match
  is selected, multiple matches require an explicit user selection.
- [x] Require a matching read-only verification receipt before `ready`.
- [x] Block required non-ready connections while permitting missing optional
  integrations to remain visible.
- [x] Add the n8n metadata discovery adapter using `list_credentials`; never
  request or retain secret data.
- [x] Persist versioned, digest-fenced setup snapshots through Captain-only
  Gateway API routes, expose an authenticated n8n UI deep-link surface, and
  emit aggregate-only Minibook projections.
- [x] Bind verified connections to typed n8n deployments and short-lived
  Captain capability leases.
- [x] Add explicit rotation/revocation transitions, project binding, expiry,
  workflow-artifact digest fencing, stale-evidence rejection, and runtime
  expiry checks.
- [ ] Prove the new MariaDB tables and feed across a real Gateway restart.
- [ ] Prove one real API-key/Bearer integration and one OAuth integration.
- [ ] Run clean-checkout, architecture, security-audit, and live evidence gates.

The checked metadata adapter has deterministic HTTP/MCP boundary coverage but
does not claim a successful live `list_credentials` call. On 2026-08-04 the
native call, `/healthz`, and MCP initialize all timed out. Docker Desktop logs
identified `DockerDesktop/Wsl/CommandTimedOut` while running
`wsl.exe -l -v --all`; Docker restart and a forced stop/start did not recover
the Windows WSL subsystem. No Captain or VibeMind container/volume was deleted
or reconfigured. MariaDB restart/replay, Minibook rebuild, and the provider E2E
gates therefore remain live non-claims until Windows/WSL is restarted.

## Acceptance sequence

1. Parse `TO_BE_BUILT.md` credential aliases without values.
2. Consume a Captain-released exact n8n credential type from the Tool
   Integrator result.
3. Show the user the exact credential type to create in n8n.
4. Discover sanitized credential metadata by exact type and project.
5. Require explicit selection when more than one credential matches.
6. Execute a harmless, typed read-only probe and retain only immutable evidence
   references.
7. Mark the connection ready and issue a scoped, expiring capability lease.
8. Revoke readiness immediately after rotation failure, credential removal,
   project mismatch, workflow digest drift, or failed probe.

## Non-claims

- Metadata discovery is not proof that a credential works.
- A healthy n8n instance is not provider evidence.
- A generated workflow is not authorized execution.
- This plan does not create, rotate, read, or print a secret.

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
- [x] Seal only matching n8n deployment/execution/correlation evidence into a
  project- and workflow-bound, secret-free Gateway verification receipt.
- [x] Prove the new MariaDB tables and feed across a real Gateway restart,
  including digest-fenced mutation replay, rotation, and revoke.
- [x] Prove a native Captain-n8n `list_credentials` metadata read through the
  SSE-aware adapter without exposing credential content.
- [x] Prove the independent Minibook v2 HTTP projection, restart, idempotent
  replay, drift rebuild, and redaction canaries against a Captain-compatible
  local feed.
- [ ] Prove one real API-key/Bearer integration and one OAuth integration.
- [ ] Run clean-checkout, architecture, security-audit, and live evidence gates.

The fail-closed portal live harness is now
`tests/live/test_portal_integration_live.py`. Without
`CAPTAIN_PORTAL_LIVE_E2E=1` and its complete disposable configuration group,
every case skips before network access. Its current verified-local and blocked
live status, including the exact operator commands, is recorded in
`docs/superpowers/plans/2026-08-05-self-service-integration-portal-live-evidence-gaps.md`.
The unchecked provider and clean-checkout items above remain unchecked until
that gate records real provider-backed evidence; configured URLs or metadata
discovery do not satisfy them.

On 2026-08-05 WSL and Docker recovered without deleting or reconfiguring any
Captain or VibeMind volume. Captain MariaDB's isolated `captain_test` compose
service passed the authenticated Gateway API restart/replay, rotation, and
revoke acceptance cases. Captain-n8n `/healthz`, authenticated workflow read,
and native MCP `list_credentials` each returned HTTP 200; the production
adapter correctly parsed the MCP SSE response. The local Captain-n8n instance
currently has zero `httpBearerAuth` and zero `oAuth2Api` credential metadata
entries, so the provider E2E gates remain explicitly blocked rather than
simulated. The separate Minibook live gate passes against a temporary local
Minibook HTTP service and a Captain-compatible v2 feed; it is not a claim that
a provider-backed Gateway promotion has already reached Minibook.

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

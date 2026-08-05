# Self-service integration portal design

## Purpose

Make the existing Captain integration control plane usable by a team member
without granting that person Captain, n8n, database, or repository authority.
The portal runs on the user's Mini-PC and coordinates the one permitted manual
secret step: creating or authorizing an n8n credential. It never receives,
stores, logs, or relays credential values.

## Authority and deployment

| Component | Deployment | Authority | Data it may hold |
| --- | --- | --- | --- |
| Portal | Mini-PC | authenticated user experience | user identity, organization, setup-ticket reference, redacted status |
| Supabase | Mini-PC | portal authentication and tenant isolation | users, organizations, opaque setup-ticket references |
| Captain Gateway | existing Captain service | requirements, readiness, leases, verification, promotion | versioned secret-free setup state and evidence references |
| n8n | Captain-owned n8n service | encrypted credentials and provider authentication | credential values, OAuth refresh tokens, workflow executions |
| Gitea | Mini-PC | versioned workflow and agent templates | source, version and release metadata only |

Supabase is not a lifecycle authority and does not store provider secrets.
Gitea must never contain `.env` files, credentials, access tokens, or OAuth
client secrets. The first release keeps the existing Captain-n8n service in
place; moving it to the Mini-PC is a separate deployment decision.

The Mini-PC portal reaches Captain only through a private mTLS service link.
The browser never receives a Gateway role token, mTLS private key, or direct
Gateway URL. The Mini-PC backend holds its client certificate in a local
gitignored secret mount, and the Captain-side proxy accepts only that client
certificate before forwarding portal traffic to the loopback Gateway.

## User journey

1. The user signs in to the portal through Supabase Auth and selects an
   organization and requested integration.
2. The portal requests a short-lived, opaque setup-ticket from Captain for the
   exact released requirement. The ticket binds tenant, job, correlation,
   credential alias, credential type, n8n project and expiry; it contains no
   secret values.
3. The portal shows a single action: **Connect in n8n**. It opens the n8n
   credentials screen with the required type, name and OAuth scope guidance.
4. The user creates a credential or completes the provider OAuth consent in
   n8n. n8n retains the secret material encrypted at rest.
5. The user returns to the portal and selects **Check connection**. Captain
   calls n8n MCP `list_credentials`, matching only sanitized metadata.
6. With one matching credential Captain binds its ID. With several matches the
   portal displays names and project metadata for an explicit selection.
7. Captain deploys and runs the released harmless probe. Only a typed,
   redacted receipt matching job, correlation, project, credential ID and
   workflow digest can move the connection to `ready`.
8. The portal displays `ready`, `missing`, `selection_required`,
   `verification_required`, `verification_failed`, `revoked`, or `expired`.
   Required non-ready connections prevent a capability lease and promotion.

## API boundary

The portal never talks to MariaDB or n8n's private database. It uses a
dedicated Captain portal API with the following secret-free operations:

- issue a short-lived setup-ticket for an authenticated tenant and released
  requirement;
- read the corresponding integration setup surface;
- request discovery, explicitly choose one discovered credential ID, and
  request a verification probe;
- request a rotation or revoke transition;
- read redacted operation status.

Captain validates the Supabase subject and organization mapping on every call.
Setup tickets are single-purpose, expire quickly, are audience-bound, and are
not bearer credentials for Gateway mutation or n8n MCP access. Existing
Captain-only Gateway routes remain the sole persistence writer.

## Failure handling

- Missing, mismatched, expired, revoked, or duplicated credentials remain
  fail-closed.
- A provider failure records a redacted failed receipt; it never returns raw
  response content to the portal.
- A stale ticket, wrong tenant, changed workflow digest, or project mismatch
  receives no setup mutation and no capability lease.
- OAuth callback failures return the user to the portal with a retryable
  status, not an inferred success.
- Portal or Supabase downtime cannot change setup state; Captain keeps the
  latest accepted status and blocks required work until it is ready.
- An unavailable, untrusted, expired, or wrong-client-certificate service link
  returns an unavailable portal state and cannot fall back to a public Gateway
  route.

## Security rules

- Credential creation remains in n8n UI. Captain, Hermes, Codex, portal,
  Supabase, Gitea, Gateway, Minibook, prompts, artifacts and logs receive no
  credential value.
- n8n credentials are referenced by exact ID and type; workflow exports carry
  only references.
- The portal uses tenant-scoped Supabase RLS and maps each authenticated
  subject to a Captain organization binding.
- Gitea template releases are digest-pinned before Captain can deploy a probe.
- Audit records contain only ticket ID, tenant ID, redacted action, status,
  correlation, timestamps and artifact digests.

## Acceptance evidence

1. A user from organization A cannot read or mutate organization B's setup.
2. A browser request cannot reach Gateway directly, and a request without the
   Mini-PC client certificate is rejected before Gateway application handling.
3. A valid ticket can discover metadata but cannot call Gateway mutations or
   n8n MCP operations directly.
4. One matching credential reaches `verification_required`; two require an
   explicit selection; zero stays `missing`.
5. A successful real Bearer probe and real OAuth probe become `ready` only
   with correctly fenced n8n deployment and execution evidence.
6. Rotation, revoke, expiry, workflow-digest drift and restart/resume all
   fail closed and preserve an append-only Captain history.
7. Repository, portal responses, Gateway records, Minibook projections and
   test artifacts contain no secret canary or provider token.
8. The Mini-PC portal can be restarted without changing the authoritative
   Gateway state; Gitea receives no secret-bearing files.

## Delivery order

1. Verify the reachable Mini-PC, Supabase and Gitea endpoints without
   mutation.
2. Add tenant/setup-ticket contracts and Gateway portal endpoints with RED/GREEN
   authorization tests.
3. Add the portal UI and the n8n deep-link/check/selection/rotation states.
4. Add Gitea template-release digest consumption.
5. Run the local Bearer E2E, provider OAuth E2E, restart/resume and redaction
   gates; only then claim production readiness.

# Self-service integration portal live evidence and gaps — 2026-08-05

## Outcome

The repository has a fail-closed, explicitly opted-in portal live harness at
`tests/live/test_portal_integration_live.py`. It never substitutes mocks for a
live dependency. Missing or partial configuration skips the entire module
before client construction or network access. A complete group is still
read-only-preflighted before the fixture permits a mutation.

Wire responses are parsed only through response-specific Pydantic models with
unknown fields forbidden. Denied/error responses retain status only. A
configured secret canary aborts any response containing it. Requests use fixed
five-second timeouts, a 128 KiB response cap, disabled redirect following and
fixed errors without body-bearing exception chains.

The gate was run without opt-in and skipped every case before network access.
This is verified-local safety evidence, not provider evidence.

## Evidence classification

### Verified local

- Missing `CAPTAIN_PORTAL_LIVE_E2E=1` skips before client construction.
- A partial group names only missing environment variable names, never values.
- URLs require HTTPS without credentials, query or fragment. HTTP is permitted
  only with an explicit loopback-only flag.
- Portal and Captain control origins must differ. Factory tenant binding uses
  only the dedicated Captain control origin/capability, never the browser
  portal proxy.
- Every protected health, audit, control and evidence URL must share the exact
  origin of the capability that authorizes it. No protected capability origin
  may equal the browser portal origin. Malformed ports are normalized to a
  fail-closed configuration error before client construction.
- Public and protected health references plus a correlation-bound provider
  audit are checked before any live mutation.
- Cross-tenant denial requires the same redacted provider invocation count
  before and after the rejected request and an unchanged setup revision.
- The executable scenario requires action-bound, single-use tickets; exact
  Bearer and OAuth metadata discovery/selection; restart/resume; three typed
  provider traces; OAuth consent/callback references; digest-pinned Gitea,
  Gateway decision/execution and Minibook projection/rebuild evidence; then
  monotonic rotation and revoke revisions.
- The three requested provider traces must have distinct IDs and exactly match
  the requested kind, alias and credential ID. Aggregated release evidence
  must contain exactly those traces, and the OAuth trace must carry both
  consent and callback references.
- Deterministic non-live tests cover opt-in/partial groups, unsafe URLs, origin
  separation, redirect behavior, response-size bounds, malformed bodies,
  strict DTO rejection and configured-canary rejection.

### Configured, not verified live

- Local environment inspection considered variable names only. No complete
  `CAPTAIN_PORTAL_LIVE_*` group was present.
- Therefore the new gate made no Portal, Supabase, Gitea, n8n, OAuth, provider,
  restart-control, evidence or Minibook request.
- Existing portal routes provide ticketing, metadata discovery/selection,
  rotation and revoke. That remains lifecycle surface, not credential proof.

### Blocked live

The current checkout does not implement the mandatory correlation-bound,
redacted provider-audit, provider-control, restart-control and aggregated
release-evidence endpoints. Those endpoints must prove:

- a harmless provider-backed Bearer verification probe;
- OAuth consent, exact callback completion and provider-backed verification;
- controlled Portal/Gateway restart and resume;
- three complete provider traces for one correlation ID;
- the digest-pinned Gitea release used by those traces;
- the accepted Gateway decision and execution reference;
- Minibook projection and drift rebuild for the same correlation ID.

The harness requires every URL, dedicated capability and health reference
before constructing a client. It cannot run against the currently known
environment and does not weaken assertions or invent success.

## Complete disposable configuration group

Set these only in a local, gitignored operator environment. Values must never
be committed or attached to test output:

```text
CAPTAIN_PORTAL_LIVE_E2E
CAPTAIN_PORTAL_LIVE_BASE_URL
CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL
CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL
CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_TOKEN
CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN
CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN
CAPTAIN_PORTAL_LIVE_ORG_A_ID
CAPTAIN_PORTAL_LIVE_ORG_B_ID
CAPTAIN_PORTAL_LIVE_JOB_ID
CAPTAIN_PORTAL_LIVE_CORRELATION_ID
CAPTAIN_PORTAL_LIVE_BEARER_ALIAS
CAPTAIN_PORTAL_LIVE_BEARER_CREDENTIAL_ID
CAPTAIN_PORTAL_LIVE_OAUTH_ALIAS
CAPTAIN_PORTAL_LIVE_OAUTH_CREDENTIAL_ID
CAPTAIN_PORTAL_LIVE_OAUTH_CLIENT_ID
CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL
CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL
CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL
CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL
CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL
CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL
CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL
CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL
CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL
CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_TOKEN
CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL
CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL
CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_TOKEN
CAPTAIN_PORTAL_LIVE_EVIDENCE_URL
CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL
CAPTAIN_PORTAL_LIVE_EVIDENCE_TOKEN
CAPTAIN_PORTAL_LIVE_SECRET_CANARY
```

`CAPTAIN_PORTAL_LIVE_ALLOW_LOOPBACK=1` is permitted only for an explicitly
isolated loopback deployment. The users must belong to distinct Supabase
organizations. Job, correlation, credentials and OAuth client must be
disposable and isolated from production.

## Reproduction commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_portal_live_support.py
.\.venv\Scripts\python.exe -m pytest -q --no-cov -rs -m live tests/live/test_portal_integration_live.py
```

The second command is valid for a real run only after every required seam and
disposable value exists. Absent prerequisites remain skipped, not passed.

## Single next operator action

Engineering must first implement the redacted audit/control/restart/evidence
seams above with dedicated capabilities and health endpoints. Only then should
an operator provision the disposable two-organization job, Bearer credential
and sandbox OAuth client and supply the complete gitignored live group.

## Merged production goal

The active integration-control-plane goal also owns these implementation gates;
they are not a separate follow-up project:

- [x] Keep the immutable Gitea template digest distinct from the canonical,
  credential-ID-bound n8n deployment digest.
- [x] Resolve n8n discovery and verification authority only from an exact,
  active Captain Tool Integrator lease.
- [x] Materialize exactly one type-matching credential ID/name placeholder and
  keep verified template bytes process-local.
- [x] Enable the concrete adapters in the default Gateway composition only by
  explicit, complete, fail-closed configuration.
- [x] Persist provider probe start/completion and Gitea release bindings through
  Captain's sole-writer Gateway.
- [x] Add separately authorized provider-control, restart-control, finalization,
  and read-only aggregate-evidence routes.
- [x] Persist Minibook integration-setup acknowledgement and rebuild convergence
  for the same run, job, and correlation.
- [ ] Run the isolated Bearer and OAuth paths, controlled restart/resume,
  rotation/revoke, and three distinct provider traces before any live claim.

## 2026-08-05 transport evidence checkpoint

The Captain host and the existing `offload-vm` Mini-PC now run separate,
digest-pinned `captain-portal-link` Compose projects. Local keys and the two
independent certificate-authority chains were generated into the ignored
`deploy/portal-link/.secrets/` tree with restricted file ACLs; no key entered
Git, command output, an artifact, or Gateway state.

Verified live, without provider or lifecycle claims:

- both WireGuard containers were healthy and each reported one current peer
  handshake;
- ICMP crossed `10.77.0.1/30` and `10.77.0.2/30` in both directions;
- both nginx mTLS proxies remained running with drop-all/minimal capabilities;
- an HTTP request to the Mini-PC loopback proxy crossed mTLS and WireGuard and
  returned the expected `404` from a temporary Captain HTTP upstream;
- the three Gateway control tokens are complete, pairwise distinct and stored
  only in the ignored `.env`.

The Mini-PC's existing Supabase and Gitea containers were observed healthy and
reachable on LAN HTTP ports. That is deliberately not claimed as production
portal evidence: their HTTPS origins, Supabase JWT/JWKS configuration, portal
deployment and real Gateway request still remain to be proven.

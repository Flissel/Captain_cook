# Self-service integration portal live evidence and gaps — 2026-08-05

## Outcome

The repository now has a fail-closed, explicitly opted-in portal live harness
at `tests/live/test_portal_integration_live.py`. The harness never substitutes
mocks for a live dependency and retains only response status, sanitized setup
metadata, revisions, IDs, digests, timestamps, project metadata and the
configured job/correlation identifiers. HTTP requests use fixed five-second
timeouts, a 128 KiB response cap, disabled redirect following and fixed errors
that do not include tokens or provider response bodies.

The gate was run without opt-in and skipped every case before network access.
This is verified-local safety evidence, not provider evidence.

## Evidence classification

### Verified local

- Missing `CAPTAIN_PORTAL_LIVE_E2E=1` produces
  `missing-live-prerequisites` skips before a client is constructed.
- A partial live group fails closed and names only missing environment variable
  names.
- All configured service URLs must use HTTPS, contain no credentials, query or
  fragment, and may use HTTP only for an explicit loopback test mode.
- The live client bounds request time and body size, does not follow redirects,
  never logs authorization values and normalizes transport/body failures to
  fixed messages.
- The executable scenario checks exact Captain-only tenant provisioning,
  conflicting-tenant denial, cross-tenant read/consume denial before a setup
  revision changes, action binding, ticket single-use, exact Bearer and OAuth
  metadata discovery/selection, monotonic revisions, rotation and revoke.
- Portal surface assertions reject secret-shaped response fields.

### Configured, not verified live

- The checkout currently exposes environment names for an isolated MariaDB and
  Minibook projection key, and a separate Gitea token name exists in the local
  environment. Values were not inspected or printed for this classification.
- No complete `CAPTAIN_PORTAL_LIVE_*` group was present. Therefore no Portal,
  Supabase, Gitea, n8n, OAuth, provider or Minibook request was authorized or
  attempted by the new gate.
- Existing portal routes expose tenant binding, ticket issue/consume, metadata
  discovery, explicit selection, rotation request and revoke. This is callable
  lifecycle surface, not proof that provider credentials work.

### Blocked live

The current product has no safe callable portal seam which can prove all of
the following without inspecting secrets or relying on operator narrative:

- a harmless provider-backed Bearer verification probe;
- OAuth consent, exact callback completion and provider-backed verification;
- controlled Portal/Gateway restart and resume between discovery and probe;
- three complete provider traces for one correlation ID;
- the digest-pinned Gitea release used by those traces;
- the accepted Gateway decision and execution reference;
- Minibook projection and drift rebuild for that same correlation ID.

The harness reports these as one explicit `BLOCKED-LIVE` skip even when the
basic disposable group is present. It does not weaken the assertions or invent
success.

## Complete disposable configuration group

Set these only in a local, gitignored operator environment. Values must never
be committed or attached to test output:

```text
CAPTAIN_PORTAL_LIVE_E2E
CAPTAIN_PORTAL_LIVE_BASE_URL
CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN
CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN
CAPTAIN_PORTAL_LIVE_ORG_A_ID
CAPTAIN_PORTAL_LIVE_ORG_B_ID
CAPTAIN_PORTAL_LIVE_CAPTAIN_TOKEN
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
```

`CAPTAIN_PORTAL_LIVE_ALLOW_LOOPBACK=1` is permitted only for an explicitly
isolated loopback test deployment. The two access tokens must belong to
distinct pre-provisioned Supabase organizations. The job, correlation,
credentials and OAuth client must be disposable and isolated from production.

## Reproduction commands

Prove fail-closed behavior without any live authorization:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov -rs -m live tests/live/test_portal_integration_live.py
```

Run the local regression and repository gates:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov -m "not live"
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
.\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config gateway
.\.venv\Scripts\python.exe scripts/verify_submission.py
npm --prefix portal run lint
npm --prefix portal run test -- --run
npm --prefix portal run build
```

After an operator has supplied the complete disposable group and the missing
safe evidence seam exists, run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov -rs -m live tests/live/test_portal_integration_live.py
```

## Single next operator action

Provision one disposable, two-organization Supabase tenant/job together with
one real Bearer credential and one sandbox OAuth client in the isolated n8n
project, then supply the complete gitignored `CAPTAIN_PORTAL_LIVE_*` group.
Engineering must add the one redacted evidence endpoint/control seam listed
under **Blocked live** before the complete gate can finish without a skip.

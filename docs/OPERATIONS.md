# Self-service portal operations

## Scope and prerequisites

The Mini-PC runs the static Captain integration portal and the Mini-PC half of
the private portal link. Supabase provides user and organization identity;
Gitea provides versioned, secret-free release templates. Neither service is
started, stopped or configured by Captain's portal deploy script. n8n,
MariaDB and the Captain-side Gateway are also outside that script's authority.

Install Docker Compose, WireGuard kernel support and PowerShell 7 on the
Mini-PC. Provision these public environment values locally:

- `CAPTAIN_PORTAL_URL`: the browser-visible portal origin, normally port 8088.
- `CAPTAIN_PORTAL_SUPABASE_URL`: the Supabase origin.
- `CAPTAIN_PORTAL_SUPABASE_ANON_KEY`: the public browser anon key.
- `CAPTAIN_PORTAL_GITEA_URL`: the Gitea origin used by the read-only preflight.
- `CAPTAIN_PORTAL_IMAGE_TAG`: immutable release tag or rollback tag.

No Supabase service role, Gateway role token, n8n token, Gitea token, provider
secret or OAuth client secret may enter the portal build or runtime container.

## Private transport

WireGuard uses only `10.77.0.0/30`: Captain is `10.77.0.1` and the Mini-PC is
`10.77.0.2`. Copy the example peer configurations from
`deploy/portal-link/wireguard/` and replace placeholders only in the ignored
paths below:

```text
deploy/portal-link/.secrets/mini-pc/wireguard/mini-pc.conf
deploy/portal-link/.secrets/mini-pc/captain-server-ca.crt
deploy/portal-link/.secrets/mini-pc/mini-pc-client.crt
deploy/portal-link/.secrets/mini-pc/mini-pc-client.key
```

The Captain host separately owns its WireGuard peer, server certificate/key
and Mini-PC client CA under `deploy/portal-link/.secrets/captain/`. The Captain
proxy listens only on `10.77.0.1:443`, requires the Mini-PC client certificate
and forwards only `/v1/portal/` to Gateway loopback. There is no LAN/public
Gateway fallback. The Mini-PC link listens only on `127.0.0.1:8443`.

## Tenant provisioning

An operator with Captain authority must bind each Supabase subject and
organization to the corresponding Captain tenant before a user can access a
setup. This is a Captain-only provisioning step. Supabase membership alone
does not grant Captain access, and the portal cannot create that binding.

## Validate, deploy and preflight

From a clean checkout, export the public values and install the ignored link
files. Dry-run validates both Compose documents and prints the exact bounded
service set:

```powershell
pwsh -NoProfile -File scripts/deploy-portal-mini-pc.ps1
```

Apply is explicit:

```powershell
pwsh -NoProfile -File scripts/deploy-portal-mini-pc.ps1 -Apply
```

Only `portal`, `mini-pc-wireguard` and `mini-pc-portal-link` are passed to
Compose `up`. The script never runs Compose `down`, removes volumes, or manages
n8n, MariaDB, Supabase or Gitea. The portal container is non-root and read-only;
its Same-Origin proxy is fixed to the loopback link.

The read-only preflight sends unauthenticated GET requests with five-second
timeouts and emits one redacted JSON document. It prints hosts, status codes,
bounded Gitea version metadata and readiness only; it does not print response
bodies, paths, query strings or credentials:

```powershell
pwsh -NoProfile -File scripts/portal-preflight.ps1
```

An unauthenticated portal-link result of HTTP 401 or 503 proves application
reachability without claiming authentication or provider readiness.

## Rollback

Set `CAPTAIN_PORTAL_IMAGE_TAG` to the previously accepted immutable tag and run
the same `-Apply` command. This rebuilds/recreates only the bounded portal and
Mini-PC link service set. Do not delete volumes and do not use broad Docker
prune commands. Gateway state remains authoritative and is not rolled back by
the static portal.

## Rotation and revoke

Provider credentials are created, rotated and revoked inside n8n. The portal
handles only credential identifiers and Captain setup tickets. mTLS and
WireGuard key rotation is an operator action on the ignored local files at both
peers, followed by a bounded link-service apply. Supabase signing-key rotation
is handled by Supabase and verified by Captain's bounded JWKS cache. None of
these rotations grants the portal access to secret values.

## Evidence boundary

Compose validation, static tests and a successful unauthenticated preflight are
configuration evidence only. They do not prove a live WireGuard handshake,
mTLS peer identity, tenant isolation, a real Bearer/OAuth provider probe, n8n
execution, Gateway promotion, Minibook projection, production availability or
regulated-domain fitness. Those claims require the separately authorized live
gate and its immutable Captain evidence.

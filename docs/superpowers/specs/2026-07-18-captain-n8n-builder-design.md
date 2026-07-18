# Captain n8n Builder and Hermes Readiness Design

> Design date: 2026-07-18
> Baseline: `main` at `76dc42bc2d7ba39faa5992adf16ff47e7fe4e63b`
> Delivery branch: `codex/captain-n8n-builder`

## Decision

Captain Cook receives a local, Captain-owned n8n builder stack that is fully
isolated from VibeMind's existing `vibemind-n8n` container. The new stack is
an explicit Compose entrypoint, uses a distinct Compose project name, a
dedicated PostgreSQL service and named volumes, and exposes n8n only on a
different loopback port. Captain's n8n target and the Hermes worker receive
the Captain builder URL only through an explicit `N8N_MODE=captain-builder`
configuration mode.

Hermes remains an independently versioned submodule. The parent repository
prepares its configuration, contract fixtures, capability lease wiring, and
smoke gates, but does not duplicate Hermes runtime code or modify unpinned
submodule internals. Captain remains the sole authority for released work,
capability grants, validation, and lifecycle evidence.

## Context

The existing root `docker-compose.yml` has an optional n8n service and the
default `.env.example` points at VibeMind on `http://localhost:15678`. That
is unsuitable for an autonomous local builder because VibeMind owns its
container and volumes. The Captain builder needs its own stable endpoint so a
live n8n delivery gate does not depend on an unrelated instance's health or
workflows.

The existing control plane already enforces `integration_intent=n8n`, the
`n8n-builder` capability profile, a short-lived capability grant, a generated
per-run MCP configuration, and sanitized Codex evidence. This work extends
the deployment boundary; it must not widen the capability policy.

## Topology

```text
Captain / Hermes runtime
  │  scoped URL + API key from gitignored environment
  ▼
captain-n8n-builder (127.0.0.1:5679)
  │
  ├── captain-n8n application container
  └── captain-n8n-postgres + captain-owned named volumes

VibeMind n8n (127.0.0.1:15678)
  └── untouched: no shared network, service, database, volume, credential,
      Compose project, start/stop/restart, migration, or workflow operation
```

The implementation must use `docker compose -p captain-n8n-builder -f
docker-compose.captain-n8n.yml`. It must never invoke a Compose command that
targets VibeMind's Compose project or `vibemind-n8n` container. It must never
use `docker compose down -v`.

## Components

### Captain builder Compose stack

`docker-compose.captain-n8n.yml` will contain only:

- `postgres`: a pinned PostgreSQL image, private Compose network, healthcheck,
  random database credentials injected from a gitignored environment file,
  and one Captain-named volume;
- `n8n`: a pinned n8n image, `DB_TYPE=postgresdb`, a fixed
  `N8N_ENCRYPTION_KEY`, loopback-only host mapping, a `/healthz` healthcheck,
  execution-data retention, and no host Docker socket or VibeMind mount;
- no Captain MariaDB, Mailpit, Minibook, VibeMind container, external network,
  or shared volume.

The stack binds `127.0.0.1:${CAPTAIN_N8N_PORT:-5679}:5678`. Port allocation
is checked before startup, and startup must fail before Docker creates a
resource if that port is in use. The Compose project name and the names of all
volumes are Captain-specific, making accidental cross-instance operations
detectable in tests.

### Account and API access

The bootstrap command generates missing local-only secrets with a
cryptographically secure generator and writes them only to a gitignored
`.env.captain-n8n` file with restrictive filesystem permissions where the
platform supports them. It creates the local owner account
`captain@local` using the pinned n8n version's supported first-run setup
surface, never by modifying n8n database rows directly.

After owner setup, the bootstrap creates or verifies a dedicated Captain API
key through the supported n8n owner/API surface and stores it only as
`CAPTAIN_N8N_API_KEY` in `.env.captain-n8n`. If the pinned image cannot
perform a supported non-interactive owner or API-key setup, bootstrap stops
with a precise, safe manual setup command; it must not fall back to insecure
basic authentication, a hard-coded password, or database mutation. Tests
exercise the supported route against the pinned image.

The human login remains possible with the generated owner credentials. The
runtime's default user is the Captain owner, but workflow operations still
require an active Captain release, an `integration_intent=n8n` command, and a
valid short-lived capability lease.

### Captain configuration and n8n client

The environment contract adds a separate builder mode:

```text
N8N_MODE=captain-builder
CAPTAIN_N8N_URL=http://localhost:5679
CAPTAIN_N8N_API_KEY=<gitignored>
CAPTAIN_N8N_PORT=5679
```

`N8N_MODE=external` retains the VibeMind default for existing users. The
target/client accepts the Captain builder endpoint only when the explicit mode
is selected; it rejects ambiguous combinations and never silently falls back
between endpoints. API keys, owner passwords, encryption keys, database
passwords, headers, full endpoints with embedded credentials, and raw n8n
payloads are excluded from Captain events, Codex prompts, test snapshots, and
structured logs.

### Hermes readiness boundary

The parent repository adds a small Hermes configuration adapter that derives
the n8n MCP/API environment only from a Captain-approved lease. It produces a
sanitized configuration reference containing the Captain builder server
identity, never the key itself. Hermes receives no direct Docker control and
cannot choose an endpoint or issue its own capability grant.

The Hermes submodule is prepared at its pinned commit by initializing it,
checking its worker entrypoints and focused contract tests, and recording the
exact revision in the readiness report. A later Hermes implementation branch
owns `captain_worker`, its MCP preflight, session supervision, and worker-loop
code. The parent repo owns only compatibility fixtures and integration tests.

## Lifecycle

1. `scripts/captain-n8n.ps1 init` verifies the requested loopback port is free,
   creates missing gitignored secrets, and validates the isolated Compose
   configuration.
2. `scripts/captain-n8n.ps1 start` starts only the `captain-n8n-builder`
   project, waits for PostgreSQL and n8n health, and rejects any VibeMind
   resource name in its resource inventory.
3. `scripts/captain-n8n.ps1 bootstrap` establishes the owner/API key via the
   pinned image's supported setup surface and verifies `/healthz` plus an
   authenticated harmless API read.
4. A Captain release with `integration_intent=n8n` receives a temporary
   `n8n-builder` lease. The runtime builds an isolated MCP configuration that
   names the Captain builder and injects the API key by environment reference.
5. Hermes/Codex creates and validates only a namespaced Captain workflow,
   returns workflow and execution references, and Captain records sanitized
   evidence through the gateway.
6. `scripts/captain-n8n.ps1 stop` stops only the Captain project. A separate
   explicit `reset` command is omitted from the initial delivery to prevent
   accidental data loss.

## Failure handling

- An occupied port, absent Docker engine, failed image healthcheck, failed
  owner/API bootstrap, or failed authenticated API read is an infrastructure
  failure. It fails the live gate and does not produce mocked evidence.
- n8n API timeouts are surfaced with endpoint identity and timeout class but
  without headers, tokens, owner credentials, workflow body, or execution
  payload.
- A request naming VibeMind's URL while `N8N_MODE=captain-builder`, or the
  Captain URL while `N8N_MODE=external`, fails closed before any HTTP request.
- Hermes cannot recover a worker by rerunning an uncorrelated deploy. Recovery
  remains governed by the persisted Captain session/claim evidence.

## Verification

The implementation is accepted only when all of these are true:

1. Compose configuration validates and the inventory shows only
   `captain-n8n-builder` resources; VibeMind's container ID, state, port
   binding, workflow count, and volume list are unchanged before and after.
2. A fresh Captain builder stack becomes healthy on its configured loopback
   port, owner/API bootstrap completes without committing or printing a
   secret, and the authenticated n8n API responds.
3. Unit tests prove endpoint selection, config redaction, lease enforcement,
   Compose isolation, and PowerShell argument construction.
4. Hermes focused contract/readiness tests pass at the recorded submodule
   revision; the parent compatibility gate still validates matching fixtures.
5. The live Captain n8n smoke test deploys one disposable namespaced workflow,
   observes a real execution, records correlated sanitized evidence, and
   deletes only that test workflow.
6. The existing VibeMind instance is neither contacted for write operations
   nor changed by the test; its workflows remain untouched.

## Non-goals

- Migrating, repairing, restarting, or administering VibeMind n8n.
- Sharing a database, encryption key, API key, named volume, or Docker network
  with VibeMind.
- Adding a generic n8n capability to every Codex or Hermes invocation.
- Moving Hermes worker internals from the submodule into Captain Cook.
- Treating a healthy container alone as proof of a working n8n API or live
  workflow execution.

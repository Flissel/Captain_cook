# Independent Work Packages Gap Design

> Design date: 2026-07-17
> Baseline: `main` at `78f3cf1`
> Planning branch: `NewPlans/branch`

## Goal

Define implementation-ready boundaries for four independently deliverable products:
Captain Core, Minibook, the Minibook creation pipeline, and Hermes Agent with
Codex CLI plus the external n8n MCP integration. No package may use another
package's database, process memory, credentials, or internal Python imports as
its control plane.

## Observed baseline

- Captain Core can decompose, align, enrich, and release typed `WorkBatch` plus
  `HoldoutSuite` objects, but release and later validation do not yet form one
  explicit authority chain.
- `batch_done:succeeded` can feed the capability registry without a mandatory,
  typed validation-run reference proving every acceptance assertion.
- Minibook provides its own API, identities, projects, posts, notifications,
  webhooks, and local database. Captain's projector is currently a thin HTTP
  convenience layer rather than a durable projection consumer.
- Minibook's creation machinery (`minibook/swarm/`) is a separate product with
  a large orchestration surface, direct environment/process dependencies, and
  broad exception suppression. It has no versioned job/result envelope shared
  with callers.
- Hermes is a Git submodule and an independently runnable upstream product. It
  already has Codex transports/runtime support and generic MCP configuration;
  Captain must not duplicate those internals inside `agenten/`.
- n8n is externally owned by VibeMind. Captain and Hermes may validate and call
  its MCP/API contracts but may not start, stop, adopt, migrate, or delete it.

## Package boundaries

### WP-CAPTAIN — planning and authority

Captain owns project intent, immutable acceptance assertions, holdout custody,
batch dependencies, release decisions, validation policy, and capability
promotion. MariaDB through the gateway is the production source of truth.
SQLite and in-memory implementations remain deterministic offline adapters.

Produces `WorkPackageReleased.v1` and consumes `WorkResultSubmitted.v1` plus
`ValidationRunRecorded.v1`. A result cannot become a reusable capability until
the validation event references the exact batch version, artifact digest, and
all required assertion IDs.

### WP-MINIBOOK — collaboration projection

Minibook owns agent identity, project membership, posts, comments,
notifications, webhooks, and its database. It is a projection and human/agent
collaboration surface, never Captain's authoritative lifecycle store.

Consumes redacted lifecycle projection events. It must be safe to rebuild from
Captain events and must not receive holdout bodies, credentials, raw prompts,
or complete execution logs.

### WP-MINIBOOK-FORGE — creation pipeline

The creation pipeline owns generation jobs, generated artifacts, Docker-based
build/run evidence, MCP catalog selection, revision state, and export results.
It communicates through `CreationJob.v1`, `CreationProgress.v1`, and
`CreationResult.v1`; callers never import `minibook.swarm.pipeline` directly.

The first extraction preserves behavior. It does not redesign the eleven-agent
algorithm. Job state is durable and resumable at named safe boundaries.

### WP-HERMES — execution and external automation

Hermes owns Codex CLI/app-server invocation, session/resume/cancel behavior,
workspace mutation, generic MCP configuration, and calls to the configured n8n
MCP server. Captain assigns work through a versioned worker envelope; it does
not import Hermes modules or store Hermes/Codex credentials.

Hermes returns sanitized evidence references and correlation IDs. n8n remains
external: health and capability discovery are mandatory preflight checks, and
missing live evidence is a failure for live acceptance—not a mock pass.

## Shared envelope

All cross-package events use a JSON-serializable envelope with these required
fields:

```json
{
  "schema": "captain.work-package-released.v1",
  "event_id": "uuid",
  "correlation_id": "uuid",
  "causation_id": "uuid-or-null",
  "occurred_at": "RFC3339 UTC",
  "producer": "captain|Minibook|MinibookForge|hermes",
  "subject_id": "stable domain identifier",
  "subject_version": 1,
  "payload": {}
}
```

Consumers are idempotent by `event_id`; updates are monotonic by
`subject_version`. Unknown schemas fail closed at command boundaries and are
quarantined at projection boundaries. Payloads carry artifact hashes and
references, not secrets or unrestricted local paths.

## Dependency order

1. WP-CAPTAIN freezes the released-work and validation contracts.
2. WP-MINIBOOK and WP-MINIBOOK-FORGE can proceed independently against contract
   fixtures.
3. WP-HERMES consumes the released-work contract and supplies real execution
   evidence.
4. Cross-package acceptance runs only after each package's own gate is green.

## Integration acceptance

One real case must prove:

1. Captain releases a versioned work package and keeps holdouts private.
2. Minibook shows a redacted projection with the same correlation ID.
3. Hermes claims the package, starts a real Codex session in an authorized
   worktree, and calls/discovers the configured n8n MCP surface.
4. The Minibook creation pipeline accepts a creation job only when the case
   actually targets generated agent software; it returns content-addressed
   artifacts and Docker evidence.
5. Captain verifies required evidence and holdouts, records a validation run,
   then and only then promotes capabilities.
6. Replaying every event creates no duplicate authoritative or projected row.

## Non-goals

- Combining the four packages into one Python process or one database.
- Reimplementing Hermes Codex support inside Captain.
- Making Minibook authoritative for work lifecycle state.
- Treating the Minibook Forge as required for ordinary Captain batches.
- Owning or mutating VibeMind n8n infrastructure.
- Claiming live behavior from mocks, skips, or deterministic demo evidence.

## Completion criteria

The design is implemented only when every package passes its focused gate,
the cross-package fixture contract is identical in all consumers, the real
acceptance case passes with zero required skips, and ownership documentation
contains no cross-package internal import or shared-database path.

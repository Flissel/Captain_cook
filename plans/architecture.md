# Agent-Factory architecture

## Intended data flow

```text
input.md
  -> Captain requirements/planning
  -> versioned WorkBatch + Holdout + TeamManifest
  -> gateway append-only lifecycle
  -> Hermes build/repair worker
  -> controlled Codex wrapper through n8n MCP
  -> generated AutoGen team + n8n workflow artifacts
  -> isolated validation and execution
  -> Minibook projections and immutable evidence
  -> Captain E2E/evaluation release gate
```

## Component authority

- **Captain:** orchestration, dependency DAG, retries, evidence aggregation,
  evaluation, and release decision.
- **AutoGen:** role runtime, conversations, reasoning, and model output.
- **n8n:** external integration transport and workflow execution only.
- **Hermes:** scoped generation and repair work; no release authority.
- **Codex wrapper:** supervised process boundary with allowlisted arguments,
  workspace confinement, timeout, cancellation, and redacted output.
- **Gateway/MariaDB:** append-only production lifecycle truth and fencing.
- **Minibook:** user-facing durable projection of artifacts and state, not the
  command authority.

## Required ports

1. `InputDocumentReader` -> validated input plus content fingerprint.
2. `TeamPlanner` -> versioned `TeamManifest` and work packages.
3. `IntegrationCatalog` -> existing n8n contract matches without executing
   workflows during planning.
4. `HermesBuildExecutor` -> supervised generation/repair request and result.
5. `N8nToolClient` -> versioned idempotent calls with trace and operation IDs.
6. `ArtifactValidator` -> static, policy, dependency, and isolated runtime
   evidence.
7. `MinibookProjection` -> idempotent artifact/state projection.
8. `ReleaseGate` -> explicit decision from immutable evidence.

## Current-state gap map

- Root planning and the Minibook Forge pipeline are separate composition roots
  with different models and evidence stores.
- The root delivery application service is still a placeholder; the gateway
  program is laying the durable lifecycle foundation first.
- Existing Minibook projection covers plans and assignments, not the full
  required artifact/evaluation graph.
- No canonical n8n workflow package or schema registry is tracked.
- Hermes setup exists, but no reviewed root port proves supervised Codex calls
  through n8n MCP.
- Trace propagation and release evidence do not yet cover every boundary.

## Invariants

- No component may bypass the gateway to mutate production lifecycle state.
- n8n may execute integrations but may not generate reasoning or approve a
  release.
- Generated code and workflow artifacts are immutable after their content hash
  is recorded; repairs create a new version.
- The holdout suite is never included in generation context.
- Every external side effect carries both `trace_id` and `operation_id`; replay
  of an identical operation is safe.

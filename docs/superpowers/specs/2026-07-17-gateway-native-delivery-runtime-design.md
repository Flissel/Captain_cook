# Gateway-Native Hermes Delivery Runtime Design

**Date:** 2026-07-17

**Status:** Approved design, awaiting written-spec review

**Authority:** Pipeline Design v2 plus the remediation program's D01 decision
**Integration baseline:** `feat/system-remediation-orchestration@d9a7434`

## 1. Goal

Build the missing production delivery lane that lets Captain Cook assign a
gateway-backed work batch to Hermes, supervise Codex while it builds n8n and
AutoGen artifacts, validate sealed artifacts against isolated holdouts, repair
behavioral failures at most three times, and publish only independently
validated capabilities to Minibook.

The first vertical milestone is deliberately one worker and one n8n tool. The
fleet, generated AutoGen team, capability reuse, and unattended release follow
only after that loop is proven restart-safe.

## 2. Binding decisions

1. MariaDB behind the Ledger Gateway is the only production source of truth.
   SQLite is a legacy import source and may not receive new production state.
2. Lifecycle state is derived from append-only gateway child events. Workers
   never mutate a trusted `validated` or `status` field directly.
3. Hermes owns worker lifecycle, Codex invocation, heartbeat, bounded repair,
   and workspace archival. Hermes cannot approve release.
4. Codex receives a sealed, typed context bundle and an allowlisted workspace.
   It cannot read holdout payloads, arbitrary host paths, or the complete host
   environment.
5. AutoGen owns conversation, delegation, reasoning, and tool choice. n8n owns
   deterministic integrations and credentials; it contains no autonomous
   conversation policy.
6. Minibook is a human-readable projection and validated-capability registry.
   Projection failure cannot roll back or replace gateway truth.
7. A behavioral validation failure may resume the same Codex session at most
   three times. Infrastructure failure does not consume a repair iteration.
8. Hard assertions must pass 100%. Semantic criteria require at least 80% per
   case, while safety and tool-policy criteria always remain hard assertions.
9. A release requires three consecutive clean composed E2E runs. A flaky pass
   is a failed release.
10. No mock, fake, stub, synthesized transcript, or manually asserted hash may
    satisfy a live gate.

## 3. Scope and decomposition

This design is implemented in five serial packets:

| Packet | Responsibility | Unlocks |
| --- | --- | --- |
| D01 | Freeze events, ports, security rules, file maps, and gates | D02 |
| D02 | Supervised Codex wrapper and one real n8n capability | Gate A |
| D03 | One restart-safe Hermes worker loop | Gate B |
| D04 | Three n8n tools, generated AutoGen team, holdouts, evaluation | Gate C |
| D05 | Fleet provisioning, reuse/learning projection, Minibook registry | Gates D and E |

D02-D05 are separately reviewable implementation branches. No packet may
silently broaden the previous packet's permissions or acceptance criteria.

## 4. System context

```text
input.md / canonical plan
        |
        v
Captain planning and release policy
        |
        v
Ledger Gateway -> MariaDB append-only events
        |
        +-> claimable work batches -> Hermes worker fleet
        |                              |
        |                              v
        |                         Codex supervisor
        |                          /           \
        |                   n8n target     AutoGen target
        |                       |                |
        |                       v                v
        |                 external n8n     isolated team runner
        |                       \                /
        |                        validation/evaluation
        |                                |
        +<-------------------------------+
        |
        +-> release decision -> Minibook projection/registry
```

Captain-owned local services are MariaDB and Mailpit. The external VibeMind
n8n instance, its credentials, Compose project, and volumes remain VibeMind
owned. Captain may call its MCP/REST interfaces but may not start, stop, adopt,
migrate, or delete its resources.

## 5. Canonical identifiers and trace context

Every command, event, log, artifact, and external side effect carries:

- `project_id`
- `run_id`
- `trace_id`
- `batch_id`
- `worker_id`
- `claim_id`
- `fencing_token`
- `artifact_id` and `artifact_version` where applicable
- `codex_session_id` after Codex starts
- `case_id` during validation and evaluation

`trace_id` spans Captain, Gateway, Hermes, Codex, AutoGen, and n8n. Secrets,
holdout contents, raw bearer tokens, and unrestricted prompts never appear in
this context.

## 6. Gateway event model

The existing gateway lifecycle is extended with these immutable event types:

| Event | Required payload | Writer |
| --- | --- | --- |
| `codex_task` | target, context hash, workspace ref, permissions, budget | Captain |
| `codex_session` | session ID, process ref, start/end times, exit class | Hermes |
| `artifact_built` | artifact ID/version, hash, type, sealed path | Hermes |
| `deploy` | target, artifact version, external deployment ref, result | Hermes |
| `validation_run` | layer, case IDs, assertion results, evidence refs | Tester |
| `repair_request` | iteration, failure class, redacted report ref | Tester |
| `batch_done` | `succeeded`, `failed`, `blocked`, or `escalated` | Gateway policy |
| `e2e_run` | composed run index, trace completeness, evidence refs | Evaluator |
| `evaluation` | hard result, semantic score, safety result | Quality Warden |
| `release_decision` | accepted/rejected, policy version, reasons | Captain |
| `registry_mirror` | Minibook capability ID/version and mirror outcome | Projector |

Existing `batch_claim` and `claim_heartbeat` events provide lease ownership and
fencing. The gateway rejects stale fencing tokens, events after the first
terminal transition, out-of-order validation, and `batch_done:succeeded`
without a successful validation event for the current artifact version.

An event command includes a unique `command_id`. Replaying the same canonical
command returns the original event. Reusing a command ID with different
content returns `409`.

## 7. Work-batch contract

Each gateway work batch contains:

```text
batch_id, project_id, run_id, parent_batch_id
target: n8n | autogen
dependencies[]
goal, constraints[], allowed_artifact_paths[]
input_schema, output_schema, schema_version
allowed_tools[], environment_allowlist[]
golden_cases[]
holdout_ref
hard_assertions[], semantic_rubric_version
timeout_seconds, output_limit_bytes, retry_limit=3
rollback_strategy, definition_of_done[]
```

An AutoGen batch is claimable only when every referenced n8n dependency has a
successful terminal event or resolves to a compatible validated capability.
Holdout references are opaque to Builder and Codex until the initial artifact
version is sealed.

## 8. D02: supervised Codex boundary

### Components

- `CodexSupervisor`: start, resume, poll, cancel, and classify Codex processes.
- `CodexCommandPolicy`: workspace, command, environment, timeout, and output
  limits.
- `CodexEventParser`: incremental JSONL parsing with real session discovery.
- `CodexRunRepository`: gateway client that records task/session events.
- `N8nBuildTarget`: renders a constrained n8n task and deploys through an
  allowlisted MCP or REST adapter.

### Process rules

Codex is started through an argument array, never shell-concatenated text. The
supervisor creates a process group, enforces a hard deadline, limits captured
output, terminates the process tree on timeout/cancel, and persists the last
parsed cursor before acknowledging completion.

The child environment contains only an explicit allowlist. Credentials are
resolved by the target adapter and are never embedded in the task prompt,
JSONL evidence, or generated artifact.

Success requires:

1. process exit `0`;
2. a real Codex session/thread ID in JSONL;
3. no policy or secret-scan violation;
4. at least one sealed artifact within allowed paths;
5. matching before/after Git evidence;
6. a persisted terminal `codex_session` event.

Resume uses the same session ID, batch ID, workspace identity, immutable input
hash, and artifact lineage. Any mismatch is a terminal policy violation.

### Gate A

A fresh workspace builds `score_lead`, deploys it to the permitted n8n target,
invokes it twice with the same idempotency key, and proves one observable CRM
side effect plus two correlated typed responses. The REST fallback is tested
separately from the n8n MCP path. Both paths require real n8n execution IDs.

## 9. D03: Hermes worker state machine

### Worker loop

```text
poll claimable batches
  -> claim with lease and fencing token
  -> start heartbeat
  -> materialize isolated workspace and context bundle
  -> record codex_task
  -> start/resume Codex
  -> static and secret gate
  -> seal artifact version
  -> deploy through target adapter
  -> reveal holdout reference to Tester only
  -> validate
     -> success: terminal evidence
     -> behavioral failure and iteration < 3: repair_request + resume
     -> behavioral failure at iteration 3: escalated
     -> infrastructure failure: blocked without increment
  -> archive workspace and release resources
```

### Recovery

The worker has no authoritative in-memory state. On startup it reads the
gateway projection and resumes from the last committed event:

- expired claim before Codex: requeue;
- live recorded PID owned by this worker: continue monitoring;
- missing/dead PID with session ID: resume the same Codex session;
- sealed artifact without deploy: continue deploy;
- deploy without validation: enqueue Tester;
- repair request: resume the recorded session;
- terminal batch: perform idempotent archival only.

Heartbeats extend leases. A stale worker cannot write after another worker
claims the batch because its fencing token is rejected.

### Gate B

One real Hermes worker claims, builds, validates, repairs when required, and
completes one n8n batch without manual artifact modification. A crash is
injected after a committed Codex session event; restart must continue the same
batch and session without duplicate side effects.

## 10. D04: targets, validation, and composed evaluation

### n8n target

The target produces three versioned capabilities:

1. `score_lead`: schema validation, enrichment, typed score, idempotent CRM
   sink evidence.
2. `send_followup`: policy-gated correlated Mailpit delivery.
3. `daily_digest`: deterministic aggregation and correlated Mailpit report.

Each tool echoes `case_id` and `trace_id`, declares schema version and timeout,
and either is naturally idempotent or requires an idempotency key.

### AutoGen target

Codex produces:

- `team.py` with side-effect-free `build_team() -> BaseGroupChat`;
- `team.json` as a secret-free audit artifact;
- role and tool-provenance manifest;
- termination and model configuration.

The execution harness imports generated code only in an isolated subprocess,
builds a fresh team per case, injects only the three validated tools, enforces
a case timeout, and records speakers, tool calls, handoffs, termination reason,
final output, sink state, and Mailpit evidence.

### Validation layers

1. Static: syntax, schema, import, secret, forbidden API, and artifact path.
2. Contract: input/output, idempotency, timeout, and correlation.
3. Golden: builder-visible functionality.
4. Holdout: isolated cases, literal sniffing, and mutated generalization.
5. Composed E2E: real AutoGen team calling real validated n8n tools.

Builder never receives holdout payloads. Tester retrieves them through a
role-scoped gateway route only after artifact sealing. Quality Warden owns the
final evaluation event and cannot be the Builder or Tester for that batch.

### Gate C

The generated `lead_operations_team` contains Intake, Qualification, Outreach,
and Reviewer roles. It must select the correct validated n8n tools for hot,
lukewarm, and rejected leads; prove timeout, invalid-response, and recovered
build-failure paths; and pass all hard assertions plus semantic thresholds.

## 11. D05: fleet, registry, and selective learning

All Hermes workers run the same image/configuration and differ only by
`worker_id`, credentials, leases, and local ephemeral workspace. They never
share authoritative memory.

Before building, Captain queries Minibook's validated capability registry by
goal, schemas, runtime compatibility, and assertion version. An exact match
creates `capability_reused`; any mismatch creates a new versioned build. Reuse
still runs compatibility and composed E2E validation.

Learning candidates are created only from a validated failure/correction pair.
A candidate becomes a shared Hermes skill after two independent successful
applications and Quality Warden approval. Secrets, holdouts, raw transcripts,
and environment-specific credentials are excluded.

Minibook receives idempotent projections of plans, assignments, reviews,
evaluations, release decisions, and registry records. Each projection stores
the gateway event cursor and external object ID. Failed mirrors retry from the
cursor and never change gateway state.

### Gate D

From a clean reset, Captain creates the dependency DAG; the fleet builds or
reuses three n8n capabilities and the AutoGen team; all lifecycle branches
reach green; and Minibook shows the validated release without operator edits.

### Gate E

Three consecutive clean E2E runs pass the happy path and all required failure
paths with complete trace coverage, no duplicate side effects, reproducible
setup commands, archived evidence, and a judge-facing flow under three minutes.

## 12. Security model

- Workspaces are per-attempt directories beneath one configured root.
- Generated code executes in an isolated subprocess/container with CPU,
  memory, wall-clock, output, and filesystem limits.
- The Codex environment is allowlisted; inherited host secrets are removed.
- Tool permissions come only from validated dependency manifests.
- External content is untrusted data and cannot modify system instructions.
- Artifact and evidence hashes are recomputed by Tester/Quality Warden.
- Logs are structured and redacted before persistence.
- Holdout access is role-scoped, audited, and unavailable to Builder/Codex.
- Every external side effect carries correlation and idempotency controls.
- Cancellation terminates the exact process tree and records why.

## 13. Failure classification

| Class | Examples | Repair counter | Outcome |
| --- | --- | --- | --- |
| Behavioral | contract failure, wrong tool choice, failed holdout | increment | resume same Codex session; escalate after 3 |
| Infrastructure | n8n unavailable, Docker unhealthy, network timeout | unchanged | blocked/retry after health recovery |
| Policy | secret leak, forbidden path/command, holdout exposure | no repair | terminal failed and security evidence |
| Claim | stale fencing token, expired lease | unchanged | stop worker; current owner continues |
| Ambiguous contract | incompatible schemas or acceptance criteria | unchanged | blocker requiring Captain/human decision |

Failures record the exact gate, evidence reference, safest fallback, and
required decision. They never become synthetic passes.

## 14. Exact implementation file map

### D02 — `feat/codex-n8n-gate-a`

Create or modify only:

- `agenten/execution/codex_policy.py`
- `agenten/execution/codex_events.py`
- `agenten/execution/codex_supervisor.py`
- `agenten/delivery/codex_runs.py`
- `agenten/delivery/gateway_client.py`
- `agenten/targets/__init__.py`
- `agenten/targets/n8n.py`
- `scripts/codex-session.ps1`
- `tests/execution/test_codex_policy.py`
- `tests/execution/test_codex_events.py`
- `tests/execution/test_codex_supervisor.py`
- `tests/targets/test_n8n_target.py`
- `tests/live/test_codex_n8n_gate_a.py`

### D03 — `feat/hermes-worker-loop`

Create or modify only:

- `agenten/hermes/__init__.py`
- `agenten/hermes/models.py`
- `agenten/hermes/worker.py`
- `agenten/hermes/recovery.py`
- `agenten/hermes/workspace.py`
- `agenten/hermes/heartbeat.py`
- `scripts/run_hermes_worker.py`
- `scripts/resume_hermes_worker.ps1`
- `tests/hermes/test_worker_loop.py`
- `tests/hermes/test_worker_recovery.py`
- `tests/hermes/test_workspace_policy.py`
- `tests/live/test_hermes_worker_gate_b.py`

### D04 — `feat/autogen-n8n-composition`

Create or modify only:

- `agenten/targets/autogen.py`
- `agenten/validation/static_gate.py`
- `agenten/validation/contract_gate.py`
- `agenten/validation/holdout_gate.py`
- `agenten/validation/e2e.py`
- `agenten/evaluation/models.py`
- `agenten/evaluation/runner.py`
- `tests/targets/test_autogen_target.py`
- `tests/validation/test_static_gate.py`
- `tests/validation/test_contract_gate.py`
- `tests/validation/test_holdout_gate.py`
- `tests/live/test_autogen_n8n_gate_c.py`

### D05 — `feat/hermes-fleet-release`

Create or modify only:

- `agenten/hermes/fleet.py`
- `agenten/hermes/learning.py`
- `agenten/delivery/projector.py`
- `agenten/delivery/minibook_client.py`
- `agenten/registry/client.py`
- `agenten/registry/models.py`
- `scripts/provision_hermes_fleet.py`
- `tests/hermes/test_fleet.py`
- `tests/hermes/test_learning.py`
- `tests/delivery/test_minibook_registry_projection.py`
- `tests/live/test_unattended_fleet_gate_d.py`
- `tests/live/test_release_gate_e.py`

Shared gateway contract changes required by D02-D05 are prepared as a separate
prerequisite packet before D02 and may touch only:

- `gateway/contracts.py`
- `gateway/store.py`
- `gateway/app.py`
- `tests/gateway/test_delivery_events.py`
- `tests/gateway/test_holdout_access.py`

No D packet edits `README.md`, `docs/WORKSTREAMS.md`, CI workflows, setup
scripts, or existing remediation plans. Those remain P20/P21/orchestrator
owned.

## 15. Verification commands

Every packet runs focused tests, then:

```powershell
python -m pytest -q -m "not live"
python -m compileall -q agenten gateway
git diff --check
```

Gateway-backed packets also run:

```powershell
pwsh -NoProfile -File scripts/test_gateway.ps1
```

Live gates are selected explicitly and fail rather than skip when their
allowlisted service is unavailable:

```powershell
python -m pytest tests/live/test_codex_n8n_gate_a.py -v -m live
python -m pytest tests/live/test_hermes_worker_gate_b.py -v -m live
python -m pytest tests/live/test_autogen_n8n_gate_c.py -v -m live
python -m pytest tests/live/test_unattended_fleet_gate_d.py -v -m live
python -m pytest tests/live/test_release_gate_e.py -v -m live
```

The final release gate additionally validates VibeMind n8n ownership safety,
Mailpit correlation, Minibook readback, trace completeness, artifact hashes,
three clean runs, and cleanup of Captain-owned temporary resources only.

## 16. Acceptance criteria

The gateway-native delivery runtime is complete only when:

1. `input.md` produces one canonical dependency DAG and work-batch set.
2. One Hermes worker passes Gate B without manual artifact modification.
3. Codex produces real session evidence and builds/deploys through both n8n
   MCP and REST fallback paths.
4. Three validated n8n capabilities are available to one generated AutoGen
   team and no unvalidated tool can be injected.
5. Holdouts remain unavailable until sealing and all accesses are audited.
6. Behavioral failures resume the same session no more than three times;
   infrastructure failures consume no repair iteration.
7. Crash recovery resumes from gateway events with no duplicate side effect.
8. Minibook reflects the release but cannot authoritatively change it.
9. Capability reuse performs compatibility and E2E validation.
10. Gates A-E pass, including three consecutive clean E2E runs.
11. A clean environment reproduces the judge evidence without undocumented
    manual steps.
12. Public claims remain limited to mechanisms and evidence actually proven.

## 17. Explicit non-goals

- Universal production readiness.
- Tamper-proof blockchain claims.
- Statistically proven autonomous learning.
- Moving AutoGen reasoning into n8n.
- Letting Minibook or SQLite become a second lifecycle authority.
- Adopting or deleting VibeMind n8n resources.
- Expanding Codex beyond batch-derived permissions.
- Building multiple workers before one worker passes Gate B and recovery.

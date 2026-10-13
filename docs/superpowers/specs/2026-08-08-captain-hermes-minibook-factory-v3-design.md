# Captain–Hermes–Minibook Agent Factory V3 Design

**Status:** Approved design

**Date:** 2026-08-08

## Decision

Captain remains the only lifecycle and success authority. Captain starts one
factory operation after a capability miss by issuing a typed command and a
bounded lease to Hermes. Hermes executes the factory workflow and submits one
generation job to Minibook. Minibook owns durable checkpoints, build assignment
state, package assembly, tests, and content-addressed storage. Hermes invokes
Codex as its only workspace-writing tool. Captain accepts the result only after
independent artifact readback and validation.

The existing `CreationJobV2` import path remains an import-only compatibility
path. It is not extended or reinterpreted as proof that Hermes or Minibook
generated a team.

## Problem

The repository contains most individual boundaries but not the complete live
chain:

- Captain already has factory lifecycle, lease, dispatch, and candidate
  validation concepts.
- `HermesCliFactory` can run released skills and accept typed evidence.
- Captain has both an HTTP Minibook client and a one-shot Minibook process
  adapter.
- Minibook has creation contracts, a durable job store, a scheduler, a
  checkpointed runner, a pipeline adapter, export support, and a filesystem
  CAS.
- The production Minibook application intentionally installs no creation
  runner: `_creation_runner_for` fails closed.
- The legacy direct Minibook pipeline can create code without executing the
  Captain→Hermes authority chain. A controlled `sales_pipeline_brief` run
  produced one generic agent, mock/TODO tools, a mismatched task, and no sealed
  creation result.
- Historical `succeeded` records do not by themselves prove materialized team
  bytes, live Hermes execution, or Captain readback.

V3 closes these boundaries without giving Minibook lifecycle authority or
allowing receipts to substitute for artifacts.

## Goals

- Trigger exactly one creation job from a Captain capability miss.
- Bind the job to Captain's command, lease, correlation, assertions, compiled
  specification, attempt, and idempotency key.
- Make Hermes execute the factory workflow and use Codex for every authorized
  workspace mutation.
- Make Minibook persist and resume every generation checkpoint.
- Generate the requested agents, handoffs, tools, source, tests,
  `team-manifest.json`, candidate manifest, and sealed CAS ZIP.
- Pause asynchronously for required user clarification, credentials, external
  API access, or n8n authority without keeping a worker process open.
- Accept `succeeded` only after Minibook readback and an independent Captain
  readback and validation.
- Prove the complete path with `sales_pipeline_brief` and required negative
  cases.

## Non-goals

- OpenFang registration or execution.
- Capability promotion or a `ready_to_use` claim.
- Production deployment.
- Treating Minibook, Hermes, Codex, prompts, or skills as lifecycle authority.
- Passing secret values through factory contracts, events, logs, Minibook, or
  the browser response payload.
- Reclassifying V1/V2 or historical results as V3 evidence.

## Authority and ownership

| Component | Owns | Must not own |
| --- | --- | --- |
| Captain/Gateway | capability decision, command, lease, lifecycle state, Action Inbox, final validation, resume decision | generated source mutation, provider implementation details |
| Hermes Factory Worker | released-skill execution, blueprint/tool plan, Minibook submission, Codex assignment execution, typed evidence | Captain transitions, final success, direct package promotion |
| Codex | mutations inside one authorized build workspace, local tests requested by the assignment | lifecycle state, credentials, work outside the allowlist |
| Minibook creation service | durable creation state, checkpoints, build assignments, package build/test, manifests, CAS publish/readback | Captain leases, promotion, user approval authority |
| Minibook UI/projection | redacted progress and pending-action display | storing credentials, answering authoritatively, resuming jobs |
| Captain Portal | HTML interaction for clarification and approvals | lifecycle decisions without Gateway validation |

## Contract versioning

Existing contracts retain their meaning:

- `CreationJobV1`: legacy Minibook generation.
- `CreationJobV2`: Captain-sealed source import.
- `CreationResultV1`: legacy/import result shape.

V3 introduces contracts rather than weakening old validation.

### `CreationJobV3`

`CreationJobV3` is a generation request and never contains a source archive.
It carries:

- creation job, factory job, command, correlation, and causation IDs;
- subject version, attempt, deadline, and idempotency key;
- Captain lease reference and exact lease digest;
- input, compiled specification, dependency graph, blueprint, and tool-plan
  artifact references;
- released Hermes factory skill ID, version, and digest;
- public acceptance assertion IDs;
- required agent IDs and ordered handoffs;
- required and optional tool requirements;
- build workspace policy reference;
- clarification policy and allowed Action Inbox request types;
- required output contract version.

The parent Captain and Minibook mirror validate byte-compatible JSON fixtures.
Absolute paths, raw prompts, credentials, holdouts, provider responses, and
unrestricted command fields are forbidden.

### `FactoryBuildAssignmentV2`

Minibook creates this assignment at the durable `awaiting_build` checkpoint.
It binds:

- the exact V3 creation job identity and current checkpoint version;
- blueprint, compiled specification, dependency graph, and tool-plan digests;
- an opaque workspace reference and workspace policy digest;
- permitted relative output roots and commands;
- required agents, tools, manifest, tests, and evidence;
- wall-clock, provider-cost, retry, and file-count limits;
- one effect key derived from job identity, version, attempt, idempotency key,
  and step;
- the Captain lease reference and deadline.

An assignment can be claimed exactly once. A replay returns the existing claim
or accepted effect and cannot start a second Codex session.

### `FactoryBuildEffectV2`

Hermes submits an effect only after Codex exits. The effect contains opaque
references and digests for:

- Hermes skill usage receipt;
- Codex session and process evidence;
- workspace snapshot;
- source archive;
- preliminary team manifest;
- compile and test evidence;
- declared tool-gap records;
- sanitized execution metadata.

The effect does not make the job successful. Minibook must read and verify its
referenced bytes before advancing.

### `CreationResultV2`

The terminal V3 result requires distinct references for:

- team manifest;
- candidate manifest;
- source ZIP;
- package manifest;
- Hermes usage receipt;
- Codex build receipt;
- build evidence;
- real-case test evidence;
- Minibook CAS readback receipt;
- tool gaps, if permitted by the compiled specification.

The result carries no optional shortcut by which one receipt or package
reference can stand in for the remaining artifacts.

### Action Inbox contracts

`FactoryActionRequestV1` contains:

- request, factory job, creation job, and correlation IDs;
- expected Captain subject version;
- request type (`clarification`, `credential_required`,
  `external_api_approval`, or `n8n_approval`);
- safe checkpoint reference and checkpoint digest;
- required scope, allowed decisions, display-safe message, and deadline;
- `pending`, `answered`, `expired`, or `consumed` status.

`FactoryActionResponseV1` contains:

- request ID and expected subject version;
- decision and approved scope;
- optional opaque `credential://` reference;
- display-safe answer metadata;
- answer timestamp and idempotency key.

Secret values are written through a separate Captain credential-store
boundary. The response and every downstream event contain only the credential
reference.

OpenFang can later add separate Action Inbox request types and scoped approval
receipts. No OpenFang type or behavior is implemented by this work.

## End-to-end data flow

1. Captain's capability resolver records a miss and creates one immutable
   factory command.
2. Captain issues a bounded Hermes lease and dispatches the command through the
   typed `HermesFactoryPort`.
3. Hermes verifies command, lease, released skill, input, and specification
   bindings. It produces blueprint and tool-plan artifacts.
4. Hermes maps and submits exactly one `CreationJobV3` through the Minibook
   creation HTTP boundary. Identical replay returns the same submission;
   conflicting replay is rejected.
5. Minibook persists the job before scheduling it and advances deterministic
   planning checkpoints from the compiled specification. The direct generic
   LLM input parser is not used.
6. At `awaiting_build`, Minibook persists one `FactoryBuildAssignmentV2` and
   exposes it to the bound Hermes worker. No scheduler thread waits for a model
   or browser response.
7. Hermes claims the assignment and invokes Codex in the authorized workspace.
   Only Codex mutates that workspace.
8. Hermes submits one `FactoryBuildEffectV2`. Minibook validates identity,
   effect key, lease, deadline, digests, workspace policy, and referenced bytes.
9. Minibook builds, compiles, and runs the generated team in isolation. It
   validates the required agents, handoffs, tools, tests, and tool-gap policy.
10. Minibook creates the final manifests and deterministic ZIP, publishes every
    object to CAS, reads each object back, and records readback digests.
11. Minibook emits `CreationResultV2` and no lifecycle promotion.
12. Hermes returns the result to Captain. Captain reads every required CAS
    object through its own artifact port and independently validates the
    result before recording factory success.

## Durable state machines

### Creation state

```text
queued
  -> planning
  -> awaiting_build
  -> building
  -> verifying
  -> succeeded
```

Every nonterminal state can move to `awaiting_user_input`, `blocked`, `failed`,
or `cancelled` only through a typed transition. `awaiting_user_input` resumes
to the recorded prior state and checkpoint after Captain consumes a valid
response.

### Assignment state

```text
available -> claimed -> effect_submitted -> effect_accepted
```

An expired or rejected assignment becomes terminal and cannot later accept an
effect. Crash recovery reuses the persisted claim/effect key. It does not
create another assignment.

### Action Inbox state

```text
pending -> answered -> consumed
        \-> expired
```

Captain atomically records the action request and the suspended factory
checkpoint. Hermes and Minibook return control and keep no waiting process.
Only Captain can consume a response and resume the same creation job.

## Asynchronous clarification and approval

Missing user-owned state is not converted into a mock, guessed value, or
generic failure. Hermes or Minibook emits a typed `ClarificationRequired`
observation. Captain validates it against the active job and lease, records
the last safe checkpoint, changes the job to `awaiting_user_input`, and creates
one Action Inbox request.

The Captain Portal renders pending requests as HTML. A Minibook projection may
show redacted status and a link, but it cannot receive an authoritative answer.
The browser posts the response to Captain/Gateway. Gateway rejects foreign,
stale, expired, over-scoped, or conflicting responses. An accepted response
is consumed once and resumes the same job, attempt, correlation, and effect
key.

Credential questions use the Captain credential store. The HTML submits the
secret to that dedicated boundary; the Action Inbox response receives only an
opaque credential reference. Raw credentials never enter MariaDB factory
events, Minibook, generated code, logs, CAS, or result metadata.

If no response arrives by the Captain deadline, the request expires and the
factory job becomes `blocked` with a typed reason. There is no synchronous
browser timeout fallback that silently approves a proposed architecture.

## Deterministic generation inputs

The compiled specification, not a generic LLM parser, is authoritative for
team topology. For `sales_pipeline_brief`, the expected topology is:

```text
account_researcher
  -> opportunity_qualifier
  -> deal_strategist
  -> executive_brief_writer
```

Required tools are `account_snapshot_reader` and
`opportunity_scoring_rules`. Both must be implemented and tested for this
input. `industry_signal_reader` is optional and may be absent only with a
typed optional tool-gap record. A generic `CSOAgent`, an extra agent, a missing
handoff, or model-invented scoring weights fails validation.

Hermes and Codex may enrich implementation details but cannot change these
compiled requirements. Any requested change requires a new Captain subject
version and a new assignment; it cannot mutate an in-flight job.

## Workspace and execution controls

- Captain creates an opaque authorized workspace reference and a digest-bound
  workspace policy.
- Hermes passes only the assignment and approved credential references to the
  Codex runtime adapter.
- Codex writes only approved relative paths beneath the workspace root.
- Shell commands, network access, provider budget, and wall time are bounded
  by the assignment and lease.
- Provider calls and other external effects use persistent effect keys.
- Required tests execute without production credentials and without access to
  Captain holdouts.
- A workspace snapshot is sealed before Minibook accepts the build effect.
- Build/test execution occurs in a fresh isolated directory or container.

## Package and success gates

Minibook can emit `succeeded` only if every gate passes:

1. Job, assignment, effect, and result identities and versions match.
2. Input and compiled-spec digests match Captain's command.
3. Team manifest contains exactly the required agents and handoffs.
4. Required tools exist, have typed schemas, and have passing tests.
5. Optional tool gaps match the compiled policy exactly.
6. Source and test files are present, regular, nonempty, and inside allowed
   relative paths.
7. Python source compiles and the declared build command succeeds.
8. The real-case command succeeds with the exact correlation and assertion
   set.
9. Candidate manifest binds the team manifest, tools, workflows, commands, and
   evidence.
10. The deterministic ZIP contains the same manifest and source bytes.
11. CAS publication and Minibook readback reproduce every expected digest.
12. The result contains every required distinct evidence reference.

Captain then repeats gates 1–5 and 9–12 through its own CAS reader and verifies
the recorded build/test outcomes before marking the factory lifecycle step
successful.

The following can never count as success:

- a `null` build-step output;
- a generic effect receipt without source bytes;
- a package reference without readback;
- one agent when the specification requires four;
- mock/TODO implementations for required tools;
- parser, build, or browser timeout;
- process exit without a terminal result;
- HTTP 2xx without valid typed content;
- V1/V2 or historical evidence presented as V3.

## API boundaries

Minibook's optional creation service exposes authenticated V3-compatible
operations in addition to existing routes:

- submit, read status, cancel, and read result;
- read/claim the next build assignment for one bound worker;
- submit one build effect using the assignment effect key;
- resume scheduling after an accepted effect;
- read artifact metadata needed for Minibook readback evidence.

Captain/Gateway exposes authenticated Action Inbox operations:

- list/read pending requests for the portal;
- submit a version-fenced response;
- store a credential through the dedicated credential boundary;
- consume a response and resume the factory job;
- project a redacted pending/resolved status to Minibook.

Worker endpoints require service identity and exact job/assignment binding.
Portal endpoints require user authentication and CSRF protection. Unknown
schemas fail closed.

## Error handling

Known failures map to typed, sanitized codes:

- expired/mismatched lease or authorization: `authorization_failed`;
- missing clarification or credential: `awaiting_user_input`;
- expired user request: `clarification_expired`;
- unavailable documentation: `documentation_unavailable`;
- required tool unresolved: `tool_unresolved`;
- Codex timeout/failure: `codex_failed`;
- missing or invalid build bytes: `build_failed`;
- manifest/spec/digest/readback mismatch: `validation_failed`;
- n8n/external provider failure: the provider-specific typed failure;
- unknown exceptions: redacted `internal_error` plus exception type only.

A parser, Hermes, Codex, Minibook, or Captain timeout always creates a typed
terminal or suspended record. It never leaves success implied by the last
completed receipt.

## Test strategy

### Contract and unit tests

- Parent/Minibook byte-compatible fixtures for every new contract.
- Strict forbidden-field, media-type, digest, version, and identity cases.
- State-machine tests for every allowed and rejected transition.
- Effect-key replay tests with counters and raise-on-second-call guards.
- Package validation tests for missing/extra agents, handoffs, required tools,
  empty files, null output, receipt-only output, and CAS tampering.
- Action Inbox tests for duplicate, stale, foreign, expired, over-scoped, and
  secret-bearing responses.

### Hermetic end-to-end test

The hermetic `sales_pipeline_brief` test uses the real Captain coordinator,
HTTP boundaries, Minibook SQLite store/scheduler, filesystem CAS, package
builder, and Captain readback validator. A deterministic Hermes/Codex adapter
writes real source and test bytes to an isolated workspace. The test proves:

- exactly one creation submission and one Codex effect;
- the four expected agents in fixed order;
- implemented and tested required tools;
- permitted optional tool-gap behavior;
- build and real-case success;
- Minibook and Captain readback;
- pause/resume through one credential request;
- rejection of one-agent, null-output, timeout, receipt-only, and tampered-CAS
  variants.

### Opt-in live end-to-end test

The live test requires explicitly configured Captain, Hermes, Codex, Minibook,
and isolated runtime ports. It proves a real Hermes released-skill invocation,
a real authorized Codex session, generated workspace bytes, Minibook build and
test, sealed CAS output, and independent Captain readback under one
correlation/effect identity. Missing live prerequisites fail closed or are
reported as unmet preflight; they are never replaced by the legacy parser.

The goal is not complete until this live path has succeeded for
`sales_pipeline_brief` or the user explicitly narrows the completion evidence.

## Rollout and compatibility

- V3 is opt-in and disabled unless every production port is configured.
- Core Minibook startup remains free of Forge/Docker/LLM imports when creation
  is disabled.
- V1/V2 routes and historical records remain readable.
- Captain's V3 validator rejects V1/V2 results for V3 jobs.
- No automatic migration rewrites historical results.
- No fallback invokes the direct Minibook LLM parser.
- OpenFang, promotion, and production deployment require separate designs and
  explicit authority.

## Completion criteria

This work is complete only when all of the following are current-state facts:

- Captain dispatches one authorized command after a demonstrated capability
  miss.
- Hermes executes the released factory workflow and submits one V3 job.
- Minibook durably pauses, resumes, builds, tests, packages, and reads back the
  generated team.
- Codex is the only build-workspace writer.
- The Action Inbox pauses and resumes missing credential/API/n8n decisions
  without a waiting worker or leaked secret.
- `sales_pipeline_brief` produces the exact four-agent team, required tools,
  tests, manifests, evidence, and CAS ZIP.
- Every specified negative success case fails closed.
- Captain independently reads and validates all required artifacts.
- Focused and relevant regression suites pass.
- The opt-in live chain succeeds with real Hermes and Codex evidence.
- No OpenFang registration, promotion, `ready_to_use`, or deployment is
  claimed or performed.

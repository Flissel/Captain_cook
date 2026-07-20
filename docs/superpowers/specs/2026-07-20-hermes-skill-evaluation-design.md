# Hermes Skill Evaluation and Captain Release Design

## Decision

Hermes extends an already Captain-released agent team only through versioned
skill candidates. A skill candidate may evaluate a team, diagnose failures,
run the released tests, make bounded workspace fixes, and propose a new
capability. Hermes has no authority to publish a skill, widen a capability
lease, release a new work package, or declare an agent team ready for use.

Captain remains the lifecycle authority. It validates every candidate's
source, scope, tests, evidence, and required capabilities through the Gateway
before a skill enters the shared catalog or a `ready-to-use` block is written.

## Scope

The feature implements a versioned build-test-evaluate-improve loop for
AutoGen-based Hermes teams:

1. Captain releases a bounded evaluation request for one existing work
   package, agent blueprint, workspace revision, and acceptance-test set.
2. Hermes evaluates the released team and runs only the approved diagnostics
   and test commands.
3. Hermes may apply a bounded code or skill-candidate fix in the leased
   workspace, then rerun the affected tests.
4. Hermes records a structured tool gap when the missing capability cannot be
   solved by an already released skill.
5. Captain validates the append-only evidence and accepts, requests redo, or
   rejects the candidate. Only Captain may publish a skill or emit a
   `ready-to-use` result.

The loop is not a general autonomous coding runtime. It consumes only Captain
released work and must not create work packages, publish n8n workflows,
register global tools, alter Captain policy, or access unleased services.

## Authority and Capability Model

```text
Captain release + Gateway lease
          |
          v
Hermes evaluation session
  - inspect released blueprint and skill catalog
  - run approved tests and diagnostics
  - produce bounded code or skill candidate
  - record TODO_TOOL candidates
          |
          v
Captain Gateway validation
  - validate evidence, tests, contracts, and capability need
  - publish or reject a shared skill
  - write ready-to-use only after all required evidence passes
```

An evaluation session binds one immutable `evaluation_request_digest`,
`work_package_digest`, `agent_blueprint_digest`, workspace revision, and
Captain-issued capability lease. Every write carries the session ID, causal
parent, idempotency key, schema version, and content digest. Replaying the
same session must return the same accepted evidence or fail as a conflict; it
must not duplicate fixes, tool gaps, or release decisions.

## Versioned Contracts

### Evaluation request

`HermesSkillEvaluationRequest.v1` is created by Captain. It contains only:

- opaque request, work-package, correlation, and session identifiers;
- content digests for the released blueprint, workspace baseline, test plan,
  and approved skill catalog;
- allowlisted test and diagnostic command identities;
- bounded iteration, time, and file-change budgets;
- a Captain-issued capability lease reference; and
- the required acceptance assertion identifiers.

It excludes prompt bodies, credentials, raw holdouts, absolute user paths, and
unreleased task descriptions.

### Skill candidate

`HermesSkillCandidate.v1` is a buildable skill artifact, not a published tool.
It contains a stable skill ID, semantic version, source digest, declared
inputs/outputs, allowed tool identities, required capability types, test-case
references, failure behavior, and provenance to one evaluation request. A
candidate can be local-tested by Hermes but is unavailable to all other agents
until Captain publishes its immutable digest to the skill registry.

### Tool-gap marker

`TODO_TOOL.v1` is a structured, append-only proposal created when an approved
test or diagnostic requires an unavailable capability. It records:

- a stable gap ID and severity (`required` or `optional`);
- the blocked assertion and evidence reference;
- the requested input/output contract and least-privilege capability need;
- up to three implementation options with their risk and acceptance test;
- whether an existing released skill was considered and rejected; and
- a candidate skill reference when Hermes was able to build one.

A required unresolved tool gap blocks `ready-to-use`. An optional gap remains
visible in the result but does not block acceptance when every released
assertion passes without it.

### Evaluation evidence

`HermesSkillEvaluationEvidence.v1` contains only structured, redacted
observations: iteration number, executed command identity, exit result,
assertion IDs, source/diff digests, candidate skill digest, tool-gap IDs,
diagnostic classification, and outcome. It never stores secret values, raw
terminal output, prompt text, hidden holdouts, or absolute paths.

## Evaluation Loop

For each Captain-released request Hermes performs at most the bounded number
of iterations:

1. Validate the request's schema, digests, lease, allowed commands, and
   workspace baseline before executing anything.
2. Evaluate the team against the released acceptance assertions and approved
   skills.
3. Run the relevant test or diagnostic command through the Captain-provided
   executor boundary.
4. If it fails, classify the cause as code defect, skill defect, configuration
   defect, missing required tool, missing optional tool, or unresolved
   infrastructure evidence.
5. For code or skill defects, create a bounded candidate change, run its
   declared tests, and append the resulting evidence.
6. For unavailable capabilities, append `TODO_TOOL.v1`; Hermes may create a
   local skill candidate only when the request's lease explicitly permits its
   declared capabilities.
7. Stop with `passed`, `redo`, `blocked_tool_gap`, `unresolved`, or `failed`.

Hermes never converts its own `passed` result into a release. It can only
return a candidate evidence bundle to Captain.

## Captain Gateway Validation

Captain validates a submitted evidence bundle fail-closed:

1. It verifies request, work-package, blueprint, baseline, candidate, and
   evidence digests against the released records.
2. It requires one successful, non-skipped evidence record for every required
   acceptance assertion after the final candidate revision.
3. It rejects out-of-scope file changes, command identities, capabilities,
   unknown schemas, stale leases, duplicate causal writes, and secret-bearing
   payloads.
4. It requires every `required` TODO_TOOL to be resolved by a Captain-published
   skill and a passing declared acceptance test.
5. It may publish a candidate skill only after its contract and test evidence
   pass; publication creates a new immutable registry version.
6. It writes `ready-to-use` only when all required assertions pass, no
   unresolved required gap remains, and the resulting skill/team registry
   versions are recorded in the block evidence.

Failures preserve the evidence and produce a typed `redo`, `replan`, or
`blocked_tool_gap` disposition. No missing or malformed evidence is treated as
success.

## Storage and Interfaces

The first implementation uses typed Captain domain ports and Gateway delivery
events. The Gateway remains the sole MariaDB writer. The following event types
are added as versioned delivery evidence:

- `hermes_skill_evaluation_requested`
- `hermes_skill_candidate_built`
- `hermes_skill_test_recorded`
- `hermes_tool_gap_recorded`
- `hermes_skill_evaluation_submitted`
- `hermes_skill_published`
- `hermes_ready_to_use_validated`

Minibook receives only an existing redacted projection after Gateway commit.
It has no role in validation, registry publication, or workspace access.

## Verification

The implementation is complete only when it proves all of the following:

1. Contract tests reject unknown schemas, stale leases, unapproved commands,
   invalid digests, secret-like fields, and out-of-scope changes.
2. Deterministic tests prove a passing team, code-fix retry, skill-fix retry,
   required tool-gap block, optional tool-gap acceptance, and idempotent replay.
3. Gateway tests prove Captain alone can publish a skill and write a
   `ready-to-use` validation record.
4. Architecture tests prove Hermes does not import Gateway storage, Minibook,
   or n8n implementation modules and cannot gain unleased capabilities.
5. A live gate runs a Captain-released Hermes candidate through the real Codex
   executor and local Captain n8n integration where applicable; it records
   actual test and release evidence and is never reported green when skipped.

## Non-goals

- Replacing the existing AgentFarm planning evaluator.
- Allowing Hermes to release work packages, change Captain policy, or publish
  directly to the shared skill catalog.
- Giving every skill n8n, browser, Codex, or filesystem write access.
- Treating a planned test, an LLM transcript, or a Minibook post as proof that
  a skill or team is ready for use.

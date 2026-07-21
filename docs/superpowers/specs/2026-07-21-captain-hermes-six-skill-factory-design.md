# Captain-Controlled Hermes Six-Skill Factory Design

Status: approved design

Date: 2026-07-21

## Decision

Captain will release and advance a six-skill Hermes workflow for discovering,
building, running, evaluating, improving, and reporting an AutoGen agent team.
The skills are versioned in the Captain repository under
`agenten/agent_factory/skills/` and exposed to Hermes through an external skill
directory. The live repository directory is not itself a release: Captain
binds every invocation to an immutable content digest and a short-lived,
role-specific lease.

Hermes executes one released step at a time. It may use Codex to build and
repair code, run real paid model calls when the job explicitly authorizes
them, and use Context7 or the Captain n8n MCP broker within the released
capability scope. Hermes may recommend promotion, retry, or a block, but only
Captain validates evidence, publishes a skill or capability, and records
`ready_to_use` through the Gateway.

This design specializes the existing Agent Factory, skill-evaluation, runtime
Swarm, typed n8n, and Gateway boundaries. It does not introduce a second
lifecycle or give Hermes direct ledger authority.

## Goals

1. Make the complete build-evaluate-improve process explicit as six reusable
   Hermes skills.
2. Reuse existing code, tests, AutoGen components, skills, and integrations
   before generating new code.
3. Turn each Codex delegation into a bounded, evidence-producing build job.
4. Permit real paid execution with a Captain-owned cumulative USD budget.
5. Evaluate the produced team independently against Captain-released
   assertions and private holdouts.
6. Improve code, prompts, context, tools, model clients, memory, AutoGen
   conversation patterns, and n8n nodes without weakening prior assertions.
7. Report typed results and unresolved gaps to Captain for the authoritative
   release decision.

## Existing Boundaries Reused

The workflow must compose the existing implementation rather than duplicate
it:

- `AgentFactoryJobV2`, `FactoryLease`, `FactoryEvidenceBlock`, and
  `FactoryProjection` remain the lifecycle foundation.
- `HermesSkillEvaluationRequest`, `ReleasedHermesSkill`,
  `HermesSkillEvaluationEvidence`, and `TODO_TOOL.v1` remain the released-skill
  evaluation foundation.
- `SwarmOrchestrator`, `codex.run`, `codex.resume`, capability profiles, and
  prompt policy remain the runtime execution boundary.
- `TypedN8nTool`, the Captain n8n endpoint policy, and the lease-token MCP
  broker remain the only n8n boundary.
- The Gateway remains the sole MariaDB writer and release authority.
- Minibook remains a read-only projection after an authoritative commit.
- `hermes-agent/` remains an independently versioned worker runtime.

The six workflow artifacts below are content-addressed evidence attached to
existing Factory phases. They are not six new authoritative Gateway phases.

## Source and Release Layout

```text
agenten/agent_factory/skills/
├── autogen-agent-factory/
├── captain-factory-discover/
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
├── captain-factory-brief-codex/
│   ├── SKILL.md
│   ├── references/
│   └── templates/
├── captain-factory-execute-team/
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
├── captain-factory-evaluate-team/
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
├── captain-factory-improve-team/
│   ├── SKILL.md
│   ├── references/
│   └── templates/
├── captain-factory-report-captain/
│   ├── SKILL.md
│   ├── references/
│   └── scripts/
└── captain-agent-factory-loop/
    └── bundle.yaml
```

The six `SKILL.md` files contain concise decision and execution instructions.
Typed parsing, hashing, command construction, evidence collection, and schema
validation live in shared Python helpers rather than being reimplemented in
natural language. References and templates are loaded only by the skill that
needs them.

Hermes discovers the repository skill root through `skills.external_dirs`.
For a released job, Captain supplies an immutable skill ID, version, artifact
reference, and SHA-256. Hermes must verify the loaded bytes against that
release before any effect. A changed working-tree skill is rejected as a
digest mismatch. The bundle is an operator convenience for loading related
instructions; it neither grants capabilities nor advances the lifecycle.

## Authority Flow

```text
Captain FactoryJob + released skill + role lease
  -> Skill 1 discover
  -> Captain validates CodebaseInventory
  -> Skill 2 brief and delegate to Codex
  -> Captain validates candidate/build evidence
  -> Skill 3 execute the real team
  -> Captain validates execution and budget evidence
  -> Skill 4 evaluate assertions and holdouts
  -> Captain chooses promote, improve, block, or escalate
  -> Skill 5 performs the released improvement when requested
  -> repeat build, execute, and evaluate within the same job budget
  -> Skill 6 submits typed feedback
  -> Captain Gateway validates and writes the terminal decision
```

Every skill invocation binds `job_id`, `correlation_id`, `subject_version`,
`attempt`, `skill_id`, `skill_sha256`, `input_digest`, `lease_id`, and an
`idempotency_key`. Replaying an accepted invocation returns the same output
reference. A replay with changed inputs conflicts and performs no effect.

## Job V3 and Paid Execution Policy

Paid execution is additive and must not loosen V1 or V2 parsing. A new
`captain.agent-factory-job.v3` retains the V2 bindings and its single
authoritative behavioral-iteration field, then adds a frozen
`FactoryExecutionPolicy.v1`:

```yaml
schema: captain.agent-factory-job.v3
max_behavioral_iterations: 5  # existing job-level hard ceiling
execution_policy:
  schema: captain.factory-execution-policy.v1
  mode: release                 # demo | release
  live_execution: true
  max_cost_usd: "5.00"          # decimal string, cumulative for the job
  max_runtime_seconds: 900      # cumulative live wall-clock ceiling
  required_live_runs: 3         # release=3; demo may explicitly use 1
  allowed_models:
    - approved-model-id
  sandbox_mode: workspace_write # workspace_write | isolated_danger_full_access
```

Rules:

- `live_execution=false` forbids provider, browser, computer-use, n8n
  execution, and other billable effects even when a credential exists.
- `max_cost_usd` is a positive decimal amount. Binary floating point is not
  accepted for accounting.
- Captain reserves remaining cost and time before every live call. Hermes
  cannot widen or reset the budget between attempts.
- Each provider call returns a redacted usage receipt with provider, model,
  token or unit counts, known USD cost, timestamps, and causal identifiers.
- Unknown, missing, or contradictory cost evidence yields `unresolved`; it
  never counts as a passing release run.
- Budget or runtime exhaustion stops new effects and produces
  `BUDGET_EXHAUSTED`.
- `mode=demo` may set `required_live_runs=1` and can become `demo_ready` only.
  It cannot satisfy the production `ready_to_use` promotion condition.
- `mode=release` requires three distinct successful live cases and recovery
  evidence required by the existing release policy.

## Skill 1: `captain-factory-discover`

Purpose: establish what already exists before architecture or generation.

Inputs:

- released Factory job and Agent Architect lease;
- opaque input, compiled specification, dependency graph, and workspace refs;
- released assertion IDs and repository revision;
- released skill catalog and typed tool catalog refs.

Actions:

1. Validate job, lease, workspace baseline, skill digest, and input digests.
2. Inspect worktrees, dirty state, architecture docs, package boundaries,
   dependency files, entrypoints, tests, schemas, existing AutoGen teams,
   prompts, model clients, memory, termination conditions, runtime tools,
   n8n contracts, and related TODO markers.
3. Use `rg`, language-aware imports/symbols, and targeted file reads. The
   builtin `codebase-inspection` skill may add size/language metrics but is not
   sufficient semantic inspection by itself.
4. Query AutoGen documentation through Context7 for the installed version when
   a code or architecture decision depends on it. Query n8n documentation only
   when the compiled specification declares an integration.
5. Identify reuse candidates, uncertainty, conflicts, and missing capabilities
   without changing code.

Output: `hermes.factory-codebase-inventory.v1`, including content-addressed
source observations, reusable components, relevant test/assertion mappings,
AutoGen/n8n versions and documentation provenance, tool-catalog matches,
candidate gaps, and inspected revision. It never contains source bodies,
secrets, absolute user paths, or raw terminal output.

## Skill 2: `captain-factory-brief-codex`

Purpose: turn the released goal and validated inventory into a bounded Codex
build assignment and obtain build artifacts.

Inputs:

- validated `CodebaseInventory.v1`;
- compiled Factory specification and dependency-ready work node;
- Tool Integrator lease and execution policy;
- accepted assertions and current attempt evidence.

Actions:

1. Select the smallest dependency-ready work unit.
2. Build a prompt that states the goal, measurable outcome, current evidence,
   authorized worktree, allowed files, architecture constraints, required
   tests, tool policy, documentation sources, budget, and forbidden effects.
3. Require Codex to return code, tests, manifests, digests, and command
   evidence rather than prose-only completion.
4. Delegate through `codex.run`; use `codex.resume` only for the same prompt and
   session digest.
5. Use the builtin `plan`, `test-driven-development`,
   `systematic-debugging`, and `requesting-code-review` instructions where the
   assignment requires them.
6. Seal the candidate source, team manifest, prompt/context references,
   schemas, and any n8n workflow before build validation.

Output: `hermes.factory-codex-build-assignment.v1` plus content-addressed
candidate artifacts. The assignment records prompt and context digests, not
their secret-bearing raw bodies.

The skill never passes a global approval bypass. Codex may run non-interactively
inside the exact released sandbox mode. `isolated_danger_full_access` is legal
only when the lease names a disposable worktree or container boundary and the
command cannot reach unleased services or paths.

## Skill 3: `captain-factory-execute-team`

Purpose: run the built AutoGen team against real released cases.

Inputs:

- sealed candidate and successful build evidence;
- Real Case Tester lease;
- private holdout resolver available only inside the isolated evaluator;
- live execution policy and remaining Captain budget.

Actions:

1. Re-verify all candidate, prompt, schema, tool, and workflow digests in a
   fresh runtime workspace.
2. Run compile/import and deterministic smoke gates before any paid call.
3. Assemble the declared AutoGen team with its released model clients,
   system prompts, user prompt template, context refs, memory policy, tools,
   handoffs, maximum messages, and termination condition.
4. Reserve cost and time through Captain, then execute the released case.
5. Capture structured messages, handoffs, tool-call results, termination
   reason, assertion observations, timing, and cost receipts.
6. For an n8n integration, require a scoped tool reference, matching workflow
   digest, execution ID, and bounded evidence polling. A generic workflow-ID
   executor is forbidden.
7. Do not edit candidate code during execution.

Output: `hermes.factory-team-execution-evidence.v1`. Provider errors, missing
receipts, skipped live calls, or materialization races remain explicit and do
not become successful evidence.

## Skill 4: `captain-factory-evaluate-team`

Purpose: independently measure the execution against Captain's task.

Inputs:

- immutable candidate and execution evidence;
- Quality Warden lease;
- accepted assertion IDs and private holdout refs;
- expected output and tool contracts.

Evaluation order:

1. schema, digest, lease, scope, and secret-redaction checks;
2. deterministic acceptance assertions;
3. build, test, integration, and n8n evidence checks;
4. output relevance and quality rubrics;
5. AutoGen handoff, conversation, memory, termination, and recovery behavior;
6. cost, latency, and repeated-run stability;
7. optional model-based judging after deterministic gates, never instead of
   them.

Output: `hermes.factory-team-evaluation.v1`, containing per-assertion outcomes,
failure classification, evidence refs, cost summary, regression set, and a
recommendation. The evaluator cannot modify code, prompts, assertions, or
holdouts and cannot silently weaken the released success criteria.

## Skill 5: `captain-factory-improve-team`

Purpose: repair a failed candidate using only a Captain-released improvement
request.

Inputs:

- `IMPROVEMENT_REQUESTED` block and new Tool Integrator lease;
- failed evaluation with exact assertion/evidence references;
- previous candidate and the complete prior-green regression set;
- remaining behavioral, cost, and time budget.

Actions:

1. Translate failures into the smallest bounded Codex repair assignment.
2. Permit changes only to the components implicated by evidence: agent code,
   system prompt, user prompt, context selection, tool contract, model client,
   memory configuration, AutoGen conversation pattern, handoffs, termination
   condition, n8n nodes/workflow, tests, or technical documentation.
3. Require every previously passing assertion to run again.
4. Seal the new candidate as a child of the prior candidate and record the
   precise change/evidence mapping.
5. Return to build, execution, and independent evaluation. A behavioral
   failure consumes one of at most five attempts; an infrastructure recovery
   resumes the same attempt.

Output: `hermes.factory-candidate-revision.v1`. Skill 5 cannot claim success or
publish the revision.

## Skill 6: `captain-factory-report-captain`

Purpose: submit the complete, redacted result to Captain.

Inputs:

- the latest immutable candidate, execution, evaluation, cost, and tool-gap
  evidence;
- Quality Warden lease and current Factory projection.

Output: `hermes.factory-feedback.v1` with exactly one recommendation:

- `PROMOTE_CANDIDATE`;
- `RETRY_BUILD`;
- `BLOCKED_TOOL_REQUIRED`;
- `BLOCKED_CREDENTIAL_REQUIRED`;
- `BLOCKED_INFRASTRUCTURE`;
- `BUDGET_EXHAUSTED`; or
- `MANUAL_DECISION_REQUIRED`.

The feedback binds every evidence digest and explains the recommendation using
assertion and gap identifiers. Captain independently recomputes the allowed
next transition. Hermes feedback is never a Gateway release decision.

## Factory Phase Mapping

The existing authoritative phases remain intact:

| Skill | Hermes role | Existing Factory evidence/phase |
| --- | --- | --- |
| Discover | Agent Architect | inventory attached to `BLUEPRINT_CREATED` |
| Brief Codex | Tool Integrator | candidate/build artifacts attached across `TOOL_CANDIDATE_TESTED`, `AGENT_CODE_CREATED`, and `BUILD_PASSED` or `BUILD_FAILED` |
| Execute Team | Real Case Tester | `REAL_CASE_EVIDENCE` |
| Evaluate Team | Quality Warden | `QUALITY_REVIEWED` |
| Improve Team | Tool Integrator after Captain action | begins only after `IMPROVEMENT_REQUESTED` and produces the next attempt's build evidence |
| Report Captain | Quality Warden | feedback attached to `QUALITY_REVIEWED`; Captain emits `CAPABILITY_PROMOTED`, `IMPROVEMENT_REQUESTED`, or `ESCALATED` |

Skill 1 and Skill 2 may produce multiple internal artifacts, but Captain
advances a phase only after the complete required artifact set validates.

## AutoGen Team Policy

Skill 2 records a reasoned conversation-pattern decision in the candidate:

- AutoGen `Swarm` is the default for specialist teams with explicit handoffs.
- `SelectorGroupChat` is allowed when a coordinator must dynamically select
  the next specialist.
- `RoundRobinGroupChat` is allowed only for a fixed deterministic order.
- A single agent is preferred when multiple roles add no measured value.

Every multi-agent candidate declares its agents, per-agent tools, system
prompt refs, input/output schemas, handoff allowlist, memory scope, maximum
messages/handoffs, termination conditions, and recovery path. Unknown agents,
tools, handoffs, or schemas fail closed. The Factory records the installed
AutoGen version and Context7 documentation provenance used for the design.

## Tool Resolution and n8n Policy

Required capabilities are resolved in this order:

1. released typed tool in Captain's catalog;
2. existing local code or library behind an approved adapter;
3. existing AutoGen component;
4. documented native n8n node, only for declared external integrations;
5. Captain-approved n8n MCP operation;
6. typed n8n HTTP workflow;
7. tested self-built local API adapter;
8. structured unresolved gap.

The current `TODO_TOOL.v1` remains canonical and retains severity `required`
or `optional`. The workflow additionally classifies the blocking reason in
evaluation/feedback as tool, credential, or infrastructure:

- a required missing capability produces `BLOCKED_TOOL_REQUIRED`;
- an existing tool lacking an externally supplied secret produces
  `BLOCKED_CREDENTIAL_REQUIRED`;
- implemented code whose leased service is unavailable produces
  `BLOCKED_INFRASTRUCTURE`;
- an optional improvement remains a visible optional `TODO_TOOL.v1` and does
  not block when every released assertion passes without it.

n8n is used only when the compiled specification has
`integration_intent=n8n`. Only a Tool Integrator can receive the short-lived
`n8n-builder` profile and `n8n-mcp` server. The lease may allow creation and
testing of an isolated draft; activation, production adoption, service
administration, or volume management requires a separate explicit Captain
operation and is not granted by these skills.

## Self-Built API Adapter Policy

When no released tool or documented node satisfies a required contract, Skill
5 may ask Codex to build a local adapter only if the lease authorizes it. The
adapter candidate includes:

- typed request and response schemas;
- OpenAPI or JSON Schema reference;
- authentication boundary and secret placeholders;
- healthcheck;
- timeout, retry, idempotency, and error semantics;
- unit and isolated integration tests;
- container configuration when runtime isolation requires it;
- optional n8n wrapper for the external integration boundary; and
- a Captain Tool Catalog registration candidate.

The adapter remains `TODO_TOOL.v1 severity=required status=unresolved` until
Captain validates, publishes, and releases the tool plus its acceptance test.

## Capability and Safety Policy

Permissions are per-role and per-attempt, never global Hermes configuration.
Possible leased effects include the assigned worktree, artifact directory,
Codex runtime, disposable Docker resources, isolated `captain_test` database,
browser/computer-use, Context7 reads, and the Captain n8n MCP broker. Each must
be explicitly named by the released capability profile.

The workflow never grants:

- a permanent approval bypass;
- writes outside the assigned worktree/artifact directory;
- production database access by default;
- direct MariaDB ledger writes;
- Minibook lifecycle authority;
- VibeMind n8n ownership or volume administration;
- unrestricted MCP discovery for non-n8n roles;
- secrets in prompts, artifacts, logs, or evidence; or
- permission to commit, merge, push, create a repository, or activate an n8n
  workflow unless the released job explicitly requests that external effect.

The builtin `github-repo-management` skill is used only for an explicit new
repository job. Existing-repository delivery uses `github-pr-workflow` under a
separate GitHub-capable lease. `architecture-diagram` documents an accepted
architecture but is not validation evidence.

## Failure and Recovery Semantics

- `BEHAVIORAL_FAILURE` and `TEST_REGRESSION` request Skill 5 and consume one
  behavioral iteration.
- `TOOL_REQUIRED` stops paid execution and waits for a Captain tool decision.
- `CREDENTIAL_REQUIRED` records only the credential name/reference and waits
  without consuming or inventing a secret.
- `INFRASTRUCTURE_FAILURE` preserves the attempt; after bounded recovery the
  same idempotency key resumes.
- `BUDGET_EXHAUSTED` prevents all new paid effects.
- `STALE_LEASE`, `DIGEST_MISMATCH`, `UNKNOWN_SCHEMA`, or `UNSAFE_OPERATION`
  fail closed and append redacted evidence.

Restart reconstruction uses Gateway blocks plus content-addressed artifacts.
No transcript, local process ID, mutable worktree path, or in-memory counter is
accepted as the sole recovery source.

## Verification Strategy

### Skill quality

Each skill must pass structural validation, scenario tests, and a forward test
through a fresh Hermes session. Tests prove that the skill activates for the
intended request, loads only the necessary references, calls the correct typed
helpers, and stops on missing authority.

### Contract and unit gates

Tests must cover:

- all six artifact schemas and unknown-field rejection;
- Job V3 execution-policy parsing and decimal budget accounting;
- skill digest, lease, revision, and idempotency conflicts;
- semantic discovery versus the metric-only builtin inspection skill;
- Codex prompt construction, redaction, and allowed worktree scope;
- AutoGen team manifest, handoff, memory, model, and termination validation;
- n8n gating and typed execution evidence;
- required/optional tool gaps and credential/infrastructure classifications;
- cost reservation, provider receipt aggregation, budget exhaustion, and
  unknown-cost failure;
- behavioral retry versus same-attempt infrastructure recovery; and
- Captain-only promotion.

### Deterministic end-to-end gate

A complete offline Factory job runs Skills 1 through 6 with fake provider,
Codex, Context7, and n8n ports. It proves a first-pass success, a behavioral
repair, a required tool block, optional gap acceptance, replay, restart, and
redaction without claiming a live result.

### Live gates

1. A demo gate runs one Captain-authorized paid case in a disposable worktree
   with real Codex and model clients. It may report `demo_ready` only.
2. An n8n live case is included only for an integration job and uses the
   isolated Captain instance through its leased MCP broker.
3. The release gate uses distinct cases and requires three consecutive,
   complete provider traces plus the existing recovery policy. Skips, unknown
   costs, missing execution IDs, or mocked providers cannot become green.

## Non-Goals

- Replacing the existing Factory state machine, Gateway repository, or runtime
  Swarm.
- Teaching Hermes to create its own authority, assertions, holdouts, budgets,
  or leases.
- Copying all builtin Hermes skills into the Captain repository.
- Using n8n for internal code flow when no external integration exists.
- Treating a diagram, plan, generated prompt, model judgment, or Minibook post
  as proof of a working agent team.
- Automatically publishing a self-built API, GitHub repository, skill, n8n
  workflow, or capability without an explicit Captain release action.

## Completion Criteria

The workflow is complete when:

1. all six released skills exist at the repository source of truth and Hermes
   can discover them through the configured external directory;
2. every skill invocation is digest-verified and lease-scoped;
3. the six typed artifact contracts are accepted by Captain and persisted only
   through the Gateway evidence boundary;
4. paid execution cannot exceed the job's USD, time, attempt, model, or
   capability bounds;
5. AutoGen and optional n8n candidates run through independent deterministic
   and live evaluation;
6. missing tools, credentials, and infrastructure remain distinct, actionable
   blocks;
7. restart and idempotent replay are proven;
8. `demo_ready` and `ready_to_use` are impossible to confuse; and
9. only Captain can publish the final skill/team version and promotion block.

# TO_BE_BUILT Input and Outcome Design

## Decision

`TO_BE_BUILT.md` is the canonical, immutable fachliche input for one Agent
Factory build. It describes the agent system to create in human-readable
Markdown with fixed required sections. Captain content-addresses the exact
UTF-8 bytes, derives versioned technical assertions and private holdouts, and
remains the only lifecycle and release authority.

The factory produces a reusable capability package, not merely a transcript or
generated prompt. A capability becomes `ready_to_use` only after its AutoGen
team, required n8n integrations, local adapters, real-case behavior, recovery,
restart, and three consecutive end-to-end runs are proven by Captain-owned
evidence.

The current root `input.md` describes the factory itself. It is not the
canonical per-build request defined here. Migration and compatibility for that
existing file belong in the implementation plan; no runtime may silently treat
the two contracts as interchangeable.

## Goals

- Accept detailed playbook-style requests containing teams, agents, prompts,
  workflows, integrations, resources, metrics, and examples.
- Keep the input approachable for a human while making critical sections
  deterministic and fail-closed.
- Let Captain decompose business outcomes into immutable work, assertions,
  private holdouts, leases, and bounded retries.
- Let Hermes use released skills to design, assign, evaluate, and improve work
  without gaining lifecycle authority.
- Use Minibook Forge's existing `SwarmPipeline` as the build and assembly
  executor.
- Let Codex create the concrete AutoGen package, n8n workflows, and necessary
  local code or API adapters in an isolated workspace.
- Preserve n8n as an integration engine; agent reasoning stays in AutoGen.
- Produce one content-addressed, restart-safe, auditable capability package.

## Non-goals

- Treating `TO_BE_BUILT.md` as executable code or a source of credentials.
- Requiring an n8n workflow for agents that have no external integration.
- Letting Hermes, Codex, AutoGen, the SwarmPipeline, n8n, or Minibook mark work
  as released.
- Treating generated code, mocked tests, skipped live gates, Minibook posts, or
  model claims as proof of readiness.
- Allowing indefinite build-improve loops.
- Defining provider-specific production credentials in a committed artifact.

## Canonical input contract

The canonical filename is exactly `TO_BE_BUILT.md`. Captain reads its exact
UTF-8 bytes, records their SHA-256 digest and byte length, and assigns a new
input version whenever the bytes change. A changed input cannot resume an old
build under the same subject version.

The document is narrative Markdown with required headings. Tables, nested
headings, JSON examples, prompts, and links are allowed inside those sections.
The required top-level structure is:

```markdown
# TO_BE_BUILT: [name of the agent system]

## 1. Goal and desired outcome
## 2. Helpful resources
## 3. System overview
## 4. Implementation phases
## 5. Integration inventory
## 6. Agents
## 7. Shared workflows
## 8. Security and authority boundaries
## 9. Acceptance outcomes
## 10. Stop conditions
```

Captain rejects a missing or empty required section. Additional sections are
preserved as source evidence but cannot weaken required acceptance, authority,
security, or stop rules.

### Goal and desired outcome

This section describes the business capability, users, autonomous actions,
human escalation points, and measurable definition of done. The author states
observable business outcomes. Captain translates them into technical
assertions without changing their meaning and adds private holdouts whose
bodies are never disclosed to workers.

### Helpful resources

This section may contain product documentation, API documentation, n8n
documentation, data schemas, sample processes, repositories, and reference
workflows. A link is a research source, not approval. Hermes records the source,
version or retrieval time, and digest used for a design decision. Unavailable
required documentation blocks the affected work unless a captured matching
snapshot exists.

### System overview and implementation phases

The overview describes desired teams, hierarchy, responsibilities, and the
business flow. Phases state intended delivery order and dependencies. Captain
may normalize phases into a DAG, but it must reject contradictory ownership or
cyclic dependencies instead of guessing.

### Integration inventory

Each integration entry declares:

- business purpose;
- trigger and operation;
- expected input and output;
- whether the integration is required or optional;
- allowed providers or protocols;
- credential aliases without values;
- observable success and failure behavior.

The input may recommend a product or node but cannot authorize it. Hermes and
Captain validate the selected option against current documentation, available
capabilities, security rules, and real evidence.

### Agent contract

Each requested agent contains these subsections:

```markdown
## Agent: [stable human-readable name]

### Purpose
### Responsibilities
### Input schema
### Output schema
### Handoffs
### Agent behavior and prompt requirements
### Required tools and integrations
### n8n workflow requirements
### Success metrics
### Real cases
```

`n8n workflow requirements` may explicitly state that no external integration
is required. When present, it defines trigger, input, output, idempotency key,
timeout, retry, duplicate-delivery behavior, failure state, and compensation or
escalation behavior. Hermes creates the final versioned system prompt; source
prompt material in the input remains a requirement, not executable authority.

Every real case has setup, input, expected actions, observable output, and
escalation behavior. At least one success case is required for the complete
system. Captain generates additional private cases and the controlled recovery
case.

### Security, acceptance, and stop rules

The input declares human approval boundaries, prohibited actions, protected
data, read-only systems, redaction requirements, time or cost constraints, and
external dependencies. Credential values are forbidden. Only stable aliases
such as `CRM_API_KEY` may appear.

Missing business success criteria, a required API, a required credential alias,
testable provider access, or a non-contradictory authority decision blocks the
affected build. The block records the missing item, impact, safest available
alternative, and user decision required.

## Lifecycle and component ownership

```text
TO_BE_BUILT.md
  -> Captain: content address, normalize, decompose, assert, create holdouts
  -> Captain: query ready_to_use capability catalog
     -> compatible hit: lease and run the released team
     -> miss: record ForgeRequested
        -> Hermes AgentArchitect uses released skills
        -> Hermes ToolIntegrator resolves the tool inventory
        -> Minibook Forge / SwarmPipeline executes the build graph
           -> Codex builds AutoGen source and manifests
           -> Codex builds required n8n workflows
           -> Codex builds required local tools or API adapters
        -> SwarmPipeline imports and starts the generated AutoGen team
        -> Hermes RealCaseTester runs independent real cases
        -> Hermes QualityWarden evaluates evidence and regressions
        -> Captain independently validates assertions and holdouts
           -> failed and budget remains: ImprovementRequested
           -> failed and budget exhausted: Escalated
           -> passed: three clean E2E runs plus controlled recovery
           -> passed final gate: CapabilityPromoted / ready_to_use
  -> Minibook receives a redacted, rebuildable projection
```

Captain owns intent, decomposition, assertions, holdouts, leases, immutable
blocks, state transitions, validation, capability lookup, and promotion.
Hermes owns skill-guided architecture, assignments, evaluation proposals, and
improvement proposals. The SwarmPipeline owns build sequencing and assembly.
Codex owns authorized workspace mutations. AutoGen owns agent reasoning and
handoffs in the generated product. n8n owns integration execution only.
Minibook owns human-readable projection, never lifecycle state.

## Tool and integration resolution

For each external integration, Hermes' ToolIntegrator follows this order:

1. reuse an already Captain-approved typed tool;
2. use a documented native n8n node or operation;
3. use an approved n8n MCP capability;
4. use a documented external API through a typed n8n HTTP workflow;
5. have Codex build a local typed tool, API service, or MCP adapter;
6. emit a structured tool-gap marker when a required dependency is absent.

A self-built API includes an input/output schema, authentication boundary,
health endpoint where applicable, idempotency contract, timeout, retry,
duplicate behavior, structured errors, redaction, unit tests, contract tests,
and an isolated real execution. n8n may call that API through a versioned typed
workflow. Small deterministic transforms may remain local code; agent reasoning
must not be hidden in an n8n Code node.

The lifecycle uses two severities for the contract named `TODO_TOOL`:

- `required`: a mandatory API, credential, capability, or test boundary is
  unavailable. It blocks `ready_to_use`.
- `optional`: a tested code solution satisfies all current assertions, while a
  possible future native integration is recorded for audit. It does not block
  release.

Implementing and proving a self-built API resolves the required gap. The marker
remains blocking only if a required external dependency or proof is still
missing.

## Bounded improvement and recovery

Captain permits at most five build-test-improve iterations and also enforces a
configured wall-clock budget. The first exhausted limit ends mutation and
records `Escalated`. Every new iteration must cite failed assertion IDs,
preserve prior green assertions, and produce a measurable evidence change.

Infrastructure failures may retry only within the same lease, correlation, and
idempotency boundaries. Restart and resume continue the same durable chain and
must not create a second Codex session or duplicate n8n side effects for work
already accepted.

## Capability package outcome

A successful build produces this logical package:

```text
CapabilityPackage
|-- team-manifest.json
|-- autogen/
|   |-- agents and externalized prompts
|   |-- handoffs and termination rules
|   `-- state and resume logic
|-- n8n/
|   |-- versioned importable workflow JSON
|   `-- typed idempotent tool contracts
|-- adapters/
|   `-- local tools, APIs, or MCP adapters
|-- skills/
|   `-- immutable skills proven in this build
|-- tests/
|   |-- unit, schema, contract, and boundary tests
|   |-- real cases and private-holdout receipts
|   `-- controlled recovery test
|-- evidence/
|   `-- redacted content-addressed Captain evidence references
`-- RUNBOOK.md
```

A Hermes skill may enter the reusable released-skill catalog only after its
digest-matching use receipt, resulting artifact, real tests, and Captain
validation are linked. Merely generating or saving a skill candidate is not a
release.

## Ready-to-use gate

Captain may record `CapabilityPromoted` with `ready_to_use` only when all of the
following are true:

- the team manifest, role graph, schemas, prompts, and handoffs validate;
- generated AutoGen code exists, imports, and starts in isolation;
- required n8n workflows are importable, versioned, typed, idempotent, and
  executed with real provider evidence;
- local tools and self-built APIs pass unit, contract, failure, and isolated
  execution tests;
- no `required TODO_TOOL` remains open;
- an independent real business success case passes;
- a controlled failure produces the required recovery or escalation;
- restart and resume preserve correlation, command, artifact, and session
  identity without duplicate effects;
- three consecutive distinct E2E runs pass without test-side repair;
- every required and private Captain assertion passes;
- no previously green assertion regresses;
- the redacted Minibook projection can be rebuilt from Captain events.

## Execution outcome

Using a released capability produces a separate `ExecutionOutcome` containing:

- capability and team version;
- correlation ID and authoritative command/result IDs;
- typed business output or content-addressed output reference;
- assertion results;
- used tool and workflow versions;
- redacted evidence references;
- final execution status and escalation reference when applicable.

The execution outcome never contains credentials, raw private holdouts, full
model transcripts, or unrestricted local paths.

## Terminal states

- `ready_to_use`: the complete capability package and all release evidence are
  accepted.
- `blocked`: a required external prerequisite or explicit user decision is
  missing; work can resume after the same blocker is resolved.
- `escalated`: the five-iteration or wall-clock budget was exhausted without a
  safe release.
- `rejected`: a security, authority, schema, integrity, or non-recoverable
  quality violation makes the candidate inadmissible.

Workers cannot select these states. Captain derives them from authoritative
blocks and evidence.

## Acceptance criteria for this design

The later implementation plan must prove:

1. Captain rejects an empty, malformed, ambiguous, or credential-bearing
   `TO_BE_BUILT.md` before starting Hermes or the SwarmPipeline.
2. A valid playbook-style input deterministically produces source evidence,
   technical assertions, private holdout references, and a dependency DAG.
3. A capability hit reuses a released team; a miss invokes the existing
   Minibook Forge `SwarmPipeline` exactly once per idempotent creation job.
4. Hermes uses digest-verified released skills and assigns only lease-scoped
   Codex work.
5. n8n is used only for declared external integrations after documentation and
   capability discovery.
6. Missing native integration selects a tested local API/tool path or produces
   a correctly classified `TODO_TOOL` marker.
7. Generated AutoGen code, n8n workflows, and adapters exist as versioned code
   and pass their required real and isolated tests.
8. Failed assertions drive bounded targeted improvement without weakening
   assertions or duplicating accepted side effects.
9. Exactly three clean E2E runs plus one controlled recovery scenario are
   linked by complete Captain evidence before promotion.
10. The final capability package is reusable, restart-safe, and projected to
    Minibook without leaking secrets or lifecycle authority.


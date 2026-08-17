# Architecture: extension points

## Agent-Factory process boundaries

## Hermes skill-evaluation release path

Captain/MariaDB is the lifecycle authority for Hermes skill evaluation.  Hermes
may use exactly one Captain-released, digest-verified skill under a short-lived
Factory lease; it can build, test, and retain an immutable private candidate,
but it cannot publish a shared skill or write a `ready_to_use` block.

```text
request -> skill usage -> build/test evidence -> candidate retained -> Gateway validation -> skill published -> ready-to-use promotion
```

The Gateway accepts Hermes evidence only when its Factory lease, job,
correlation, subject version, content references, and digests match.  A
required `TODO_TOOL.v1` remains visible and blocks publication/promotion;
an optional gap remains audit evidence without bypassing the released
acceptance assertions.  Captain alone publishes a evaluated candidate and
records the final promotion.  The `hermes-agent/` submodule is a leased worker
runtime and never a shared-registry writer.

### Discovery-to-Codex brief authority

The V3 build path separates model judgment from authoritative artifact
construction. The Agent Architect uses the released discovery skill and returns
a digest attestation; Captain materializes and durably replays the typed
Attempt-1 inventory. A later Tool Integrator lease loads that exact inventory,
and Captain builds `FactoryBuildAssignmentV1`, the bounded Codex prompt, and
`CodexBuildBriefV1` in its private content-addressed store. Hermes applies brief
skill release v2 without tools and returns only a digest-bound attestation.
Captain then gives the exact brief to the separately leased Codex seal step.

```text
Hermes discovery attestation
  -> Captain typed inventory + durable replay
  -> Captain V3 assignment and Codex brief
  -> Hermes brief digest attestation
  -> Captain Codex execution/seal
  -> Minibook Forge import of sealed source bytes
```

Retries reuse the Attempt-1 inventory and add only the exact Captain improvement
authorization, failed evaluation, prior candidate, and regression guards. A
missing/failed replay, changed digest, stale lease, or malformed attestation
stops before Codex. Hermes never authors Captain lifecycle identity or release
evidence.

When the typed assignment declares an n8n integration, the technical workflow
construction protocol comes from the commit-pinned official `n8n-io/skills`
plugin. Hermes and Codex start with `using-n8n-skills-official` and use the
approved instance-level MCP for SDK inspection, node discovery, validation,
write, and read-back. Those skills replace locally duplicated n8n build advice;
they do not grant a lease, increase a budget, validate evidence, authorize a
retry, or promote a candidate. All of those decisions remain Captain-owned.

### Business benchmark release validation

Every V3 Factory candidate must pass a Captain-owned paired business benchmark
before promotion. The product composition is
`BusinessBenchmarkFactoryComposition`; it connects existing ports and does not
create a second lifecycle authority:

```text
private suite reference
  -> 15 paired candidate / single-agent baseline executions
  -> private immutable run and case receipts
  -> deterministic redacted aggregate summary
  -> Gateway summary commit
  -> TeamEvaluationV1
  -> FactoryFeedbackV1
  -> Quality reviewed
  -> Gateway release validation
  -> Captain capability promotion
  -> redacted Minibook aggregate projection
```

The summary is committed before the evaluation so the evaluation can reference
an exact, resolvable artifact. Captain/Gateway alone accepts that summary,
recomputes the release decision, and writes `ready_to_use`. A failed but exactly
bound summary may reach quality review so Captain can issue a bounded
improvement; it can never reach promotion. Direct release or promotion bypasses
fail closed.

Claims uses profile `insurance_claims_resolution_swarm`; Renewal uses
`customer_renewal_orchestration_team`. The selected profile, suite version,
model version, redaction policy, baseline policy, per-case cost/latency limits,
and job-wide budget are immutable inputs. Each suite has exactly 15 private
cases: three each for ordinary, boundary, incomplete, contradictory, and
mandatory-escalation behavior. Retry/resume reuses the durable effect identity
and provider fence for the same attempt; behavioral improvement creates the
next candidate attempt and remains bounded by the job's five-attempt ceiling.

The policy may treat relative candidate/baseline cost and latency ratios as
diagnostics while retaining the absolute per-case ceilings as hard stops. That
mode is valid only with a predeclared material-value gate: the candidate must
meet at least one configured correctness or completion uplift threshold, may
not be worse than the baseline, and must still satisfy candidate safety and
Captain-review requirements. Default policy serialization preserves the legacy
hard relative-efficiency gates; diagnostics and uplift thresholds are explicit,
digest-bound opt-ins.

Only aggregate disposition/reason codes, correctness and completion basis
points, cost/latency ratios, unsafe-tool and missed-handoff counters, summary
digest, and correlation cross into Minibook. Case identifiers, inputs,
expectations, rationales, run receipts, prompts, provider output, and credentials
remain private. Synthetic/anonymized benchmarks are release validation, not
evidence of regulated-domain accuracy or production fitness by themselves.

The deterministic integration path uses sealed candidate archives and the
Gateway repository. Provider-backed Gate E is separate. Gate E verifies the
generic provider-backed delivery release policy: three distinct clean delivery
batches with Codex, artifact, deploy, live validation, and registry-mirror
evidence. It does not execute the Task 7 Hermes skill fixture or Factory
promotion chain. The Task 7 chain is covered by the deterministic integration
and, when explicitly configured, the isolated MariaDB integration. Missing
prerequisites are reported as skips or blocks, never as deterministic success.

`CAPABILITY_PROMOTED` consumes the latest Gateway-accepted
`FactoryReleaseDecision`.  The Gateway recomputes that decision from the stored
skill evaluation plus the submitted E2E records.  A missing or blocked decision
fails closed; only recovery evidence followed by three consecutive successful
normal E2E runs can produce the `ready` decision used by Captain promotion.

### Verification command sequence

Run the deterministic focused integration first, then the related offline
Factory/Gateway regressions:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/integration/test_hermes_skill_evaluation_gateway.py
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory tests/gateway/test_factory_repository.py tests/gateway/test_delivery_events.py tests/agent_runtime/test_n8n_endpoint.py
```

The direct MariaDB path is separate and destructive only to the disposable
test database. `TEST_MARIADB_DSN` must target the exact isolated database
`captain_test`; the guard rejects every other database name. Supply the DSN
only through the local environment, then run the DB-backed integration files:

```powershell
$env:TEST_MARIADB_DSN = "<isolated MariaDB DSN ending in /captain_test>"
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/integration/test_hermes_skill_evaluation_gateway.py tests/gateway/test_agent_factory.py
```

After all database-resetting tests, the provider-backed release gate remains
explicitly opt in:

```powershell
pwsh -NoProfile -File scripts/run-gate-e.ps1
```

Gate E requires the prepared Captain test database, Codex/provider
configuration, and the approved local n8n MCP broker. It provides generic
delivery release evidence, not Task 7 Factory-chain evidence. Missing
prerequisites are a skip or block, never a green Gate E.

The Agent-Factory path is split into three independently composed process
contracts. They exchange frozen, versioned data; none imports the next stage's
implementation. The local candidate runs these stages in one Python runtime;
production OS-process isolation remains a release gate below.

```text
UTF-8 input.md
  -> MarkdownProjectInputParser
     (exact bytes, SHA-256, safe logical source name, heading outline)
  -> CaptainPipeline.compile
     (decompose, align, enrich, policy-check; no publication)
  -> CanonicalPlanCompiler
     (stable topological order, one disposition, five-worker pool, handoffs)
  -> CanonicalPlanPublisher
     (one atomic source + plans + contracts + isolated holdouts bundle)
  -> PlanReviewProcess
     (immutable plan in, typed findings and review_id out)
  -> trusted ReviewDecisionReader
  -> ExecutionProcess
     (review, capability, dependency, and validation projections required)
  -> ArtifactReviewProcess
     (content-addressed references only; no build workspace path)
```

Ownership is enforced as follows:

- `agenten/planning/input_parser.py` performs deterministic input I/O and has
  no LLM, Docker, gateway, Minibook, or execution dependency.
- `agenten/planning/captain_pipeline.py` now exposes `compile()` separately
  from its compatibility `run()` publisher path.
- `agenten/planning/canonical_contracts.py` is the cross-process contract
  module. Review and execution must not import the canonical publisher.
- `agenten/review/` can produce decisions and content-addressed findings but
  has no execution, release, delivery, gateway-write, or storage import.
- `agenten/execution/` consumes a review through a trusted read port and only
  unlocks dependencies after independent validation evidence. A static
  `satisfied_by` value is insufficient without a validated capability
  projection.

The JSON publisher is an offline evidence adapter, not production authority.
Production plan/review/capability/validation projections still have to be
implemented through the sole-writer MariaDB Ledger Gateway. In-process review
callbacks prove contract separation but do not yet prove OS-level sandboxing;
the production reviewer must run in a restricted separate process.

## Minibook collaboration projection

Minibook is a rebuildable collaboration projection, never lifecycle authority.
After an authoritative gateway commit, Captain's delivery adapter consumes a
paginated `captain.minibook-projection.v2` feed and uses only Minibook's public
HTTP projects, posts, comments, and search routes. Captain production modules
do not import `minibook.src`, its SQLite models, Hermes, or the Forge pipeline.

The projection envelope is idempotent by event ID and monotonic by subject
version. A local SQLite cursor stores only event/post identity, subject heads,
feed position, contract version, and quarantine reasons. It does not store
event bodies. Minibook persists one immutable event-to-post identity per
admitted event and a separate monotonic subject head in the same write
transaction. An expired older writer cannot overwrite a newer remote view,
and previously admitted views remain independently replayable. The projection
project has one deterministic external identity.

The v2 event payload contains no producer-supplied display text. It accepts only
enumerated template/status/actor identifiers, typed subject and batch
references, bounded versions, and content-addressed artifact digests. Minibook
revalidates that event and owns canonical titles, content, tags, fingerprints,
and labels. Its projection mutation routes require a dedicated
`MINIBOOK_PROJECTION_API_KEY`; ordinary agent credentials have no projection
write scope. The deterministic projection project ID and name are reserved
across every ordinary write surface. Direct project routes, indirect post and
webhook lookups, plan/member/admin routes, GitHub integration ingress, and
registry project linkage all fail with HTTP 403. A route-inventory fitness test
forces every new POST/PUT/PATCH/DELETE endpoint to be classified as guarded,
projector-only, or project-independent. The normalized internal service-agent
name is also reserved across public agent and registry creation; a pre-existing
collision fails closed instead of being adopted.

Rebuild is dry-run by default and creates no cursor file, directory, table, or
Minibook object. `--apply` repairs missing or modified projection posts and
retires only marked duplicates/orphans, leaving unrelated Minibook content
untouched. An unversioned v1 cursor, active v1 post set, or interrupted cutover
requires an explicit full rebuild. The full rebuild validates the complete v2
feed and transactionally adopts a verifiable historical v1 project that used a
random ID. Before any write, Minibook validates every candidate's complete v1
identity-tag grammar and stored content hash, rejects duplicate event or subject
versions, and requires every projection fence, event-post, and subject-head
reference to resolve consistently. It repeats that read-only preflight under the
SQLite write lock before adoption. Only fully verified v1 posts move to the fixed
singleton; human posts, memberships, and integrations remain on the
deterministically renamed legacy project. It then replays one deterministic post
per event, retires v1 posts, and atomically checkpoints the terminal cursor plus
`contract_version=v2` only after convergence. Repair uses the structured
canonical event upsert. Retirement is
a separate projector-authenticated endpoint accepting only the fixed
`duplicate`, `orphaned`, or `v1-cutover` reason enum; Minibook replaces title,
content, tags, status, mentions, pinning, and integration references with its
canonical retired representation.

For a Factory promotion, cursor advancement additionally requires a durable
Minibook read-back and a Captain-owned
`captain.minibook-projection-acknowledgement.v1`. The acknowledgement binds the
promotion event, correlation, subject version, deterministic project/post
identity, canonical content hash, and Minibook creation time. The Gateway
reconstructs the authoritative promotion and benchmark aggregate before it
accepts the acknowledgement, then appends one idempotent `registry_mirror`
delivery event. A missing, drifted, duplicated, stale, or rejected projection
therefore leaves the feed cursor uncommitted and never becomes release
evidence. Minibook remains projection-only; it does not authorize promotion or
write Captain lifecycle state.

Minibook starts independently with `python run.py`. Its health gate requires no
Captain, Hermes, Codex, Docker, Forge, or n8n process. The separate live replay
gate starts that package command with a dedicated projection credential, reads
a redacted event from a synthetic local HTTP feed implementing Captain's v2
contract, restarts the projector, mutates and rebuilds the view, and requires
zero skips. It does not claim a deployed Captain gateway feed.

## Agent-runtime control plane

`agenten/agent_runtime/control_plane.py` composes the reviewed Hermes planning,
Captain compilation, swarm scheduling, runtime-tool, capability, and validation
ports. Captain remains lifecycle authority: it compiles and releases the
versioned DAG, derives every code or n8n capability profile from the released
batch, owns the behavioral retry budget, and decides `passed`, `redo`, or
`replan`. Hermes supplies a versioned plan and agent-blueprint references;
Minibook contributes only the collaboration-post reference.

For each correlation ID, the control plane persists an atomic checkpoint before
and after effects. The checkpoint binds the Hermes plan digest, the canonical
public compiled-batch digest, task order, opaque `workspace://` references,
content-addressed prompt references, results, and public validation records. It
contains neither prompt bodies nor private holdout cases. Infrastructure failures
have a separate finite retry budget and never consume a behavioral iteration.
Validation records must name exactly the acceptance assertions Captain released.
An unchanged terminal checkpoint produces a byte-stable evidence manifest after
restart.

The swarm receives only dependency-ready task projections. `codex.run` and
`codex.resume` command identities include the prompt digest, so a validation
artifact can resume the same session without colliding with the original
command. A code-builder grant has no MCP servers. Only a Captain-released
`n8n-builder` task receives the short-lived `n8n-mcp` lease; this does not grant
permission to start, stop, adopt, or migrate the VibeMind-owned n8n service or
its volumes.

The correlation-indexed manifest contains typed Hermes, Captain, Codex/n8n, and
validation observations plus artifact/evidence digests. Model validation rejects
secret-like fields and values, raw authorization material, and absolute user
paths. It is an evidence projection, not a second source of lifecycle truth.

Verification is split deliberately:

- `tests/integration/test_agent_runtime_control_plane.py` uses the real Captain
  compiler, runtime service, tools, and swarm with strict deterministic external
  ports. It proves ordering, lease derivation, restart recovery, redo/replan, and
  redaction without claiming external execution.
- `tests/live/test_agent_runtime_n8n_live.py` uses the Hermes worker adapters and
  a real Codex CLI in disposable Git repositories. Its n8n case discovers the
  scoped tools, validates an SDK workflow, creates/tests/publishes/executes it,
  records real call and execution evidence, and archives the isolated workflow.
  The live file uses a strict Minibook planning-port test double; the independent
  Minibook HTTP/restart behavior is proven by its own live projection gate.

This project has two things that are meant to grow over time: the **ledger**
(`blockchain/`) that records what tasks/decisions exist, and the **agent
logic** (`agenten/`) that produces and refines them. Both were previously
hardcoded to one shape; this doc describes the seams that now let you extend
either one without editing the core classes.

## Blockchain: adding a new record type or storage backend

`Block` no longer has fixed `task`/`assigned_agents` fields. It carries:

- `block_type: str` — a free-form tag, e.g. `"task"`, `"research_result"`, `"decision"`
- `data: dict` — whatever payload that block type needs
- `metadata: dict` — optional side information that isn't part of the hash-relevant payload

To add a new kind of record, don't touch `Block` or `Blockchain` — just call:

```python
captain.blockchain.add_block(
    block_type="research_result",
    data={"query": "...", "top_url": "...", "score": 0.83},
    parent_index=project_block.index,
)
```

and look records up later with `blockchain.get_blocks_by_type("research_result")`.

The old task-shaped API still exists as a convenience wrapper:
`Blockchain.add_task_block(task, assigned_agents, status, parent_index)`
(this is what `CaptainAgent.add_task_to_blockchain` calls).

Persistence is pluggable via `blockchain/storage.py`'s `LedgerStorage`
interface. `JSONFileStorage` (the default) and `InMemoryStorage` (for tests)
are provided; to back the ledger with a database, implement `LedgerStorage`
(`load`/`save`/`clear`) and pass it to `Blockchain(storage=...)` — no other
code needs to change.

## Agent logic: adding a new AgentChat workflow

Every existing "Generator critiques with a Critic, refines, and produces a
Structured output" pipeline (project definition, project structuring,
system-prompt generation, subtask decomposition) is described once in
`agenten/workflows/base.py` as `NestedChatWorkflow`. The name remains for
compatibility, but execution is now sequential `AssistantAgent.run` calls
from AutoGen 0.7 AgentChat rather than the removed `pyautogen` nested-chat
API.

To add a new workflow:

```python
# agenten/workflows/my_workflow.py
from .base import AgentRoleSpec, NestedChatWorkflow, WorkflowStep
from .registry import register_workflow

@register_workflow("my_workflow")
def build() -> NestedChatWorkflow:
    return NestedChatWorkflow(
        name="my_workflow",
        roles=[
            AgentRoleSpec("generator", "You do X."),
            AgentRoleSpec("critic", "You critique X."),
            AgentRoleSpec("user_proxy", "You orchestrate.", kind="user_proxy"),
        ],
        steps=[
            WorkflowStep(recipient_role="generator", message="Do X for: {input}"),
            WorkflowStep(recipient_role="critic", message="Critique the above."),
        ],
        entry_role="generator",
        trigger_role="user_proxy",
        kickoff_message="Start on: {input}",
    )
```

Then add the module to the import list in `agenten/workflows/__init__.py`
(so the `@register_workflow` decorator runs), and run it from anywhere with:

```python
captain.run_workflow("my_workflow", context={"input": "..."})
```

`WorkflowStep.message` can be a `{placeholder}`-templated string (filled
from `context`) or a callable `(recipient, messages, sender, config) -> str`
for dynamic reflection messages — see `reflection_message`/`update_message`
in `base.py` for reusable examples, or write your own.

## Tools: adding a new agent capability

`agenten/tools/base.py` defines a `Tool` ABC (`name` + async `run(...)`).
Register one on a Captain with `captain.register_tool(MyTool())` and it's
available via `captain.tools.get("my_tool")` — no `create_<tool>` method
needs to be added to `CaptainAgent`. `InternetSearchTool` is the existing
example, wrapping `InternetSearcher`.

## Known gaps (not touched by this refactor)

- `blockchain/web_scamler.py` is a standalone URL-relevance service and is
  not wired into the event-driven runtime yet.
- `chats/project_maker.py` is a compatibility wrapper; the canonical project
  definition workflow is `agenten/workflows/project_definition.py`.
- Root dependencies are pinned in `requirements.txt`. The next packaging
  step is moving the root modules under an installable `src/` package without
  changing the domain/event interfaces.

### Ledger integrity boundary

Two ledgers with different guarantees share one `Block` type.

The MariaDB gateway ledger is append-only in the strong sense: `GatewayStore`
is the sole writer, issues no `UPDATE` or `DELETE` against `blocks`, and
serializes appends via `SELECT ... FOR UPDATE` on the chain tip. It can be
checked with `blockchain.Blockchain_modell.verify_chain`.

The in-process pipeline ledger (`LedgerRecorderAgent`) mutates `data`,
`status`, `metadata` and `children` in place after append and does not
recompute hashes. Its integrity guarantee is single-writer discipline plus
validated stage transitions, not hash verification. `verify_chain` is
expected to fail against it and must not be called there.

`status`, `children` and `metadata` are outside `Block.compute_hash` in both
cases, so neither ledger detects edits confined to those fields.

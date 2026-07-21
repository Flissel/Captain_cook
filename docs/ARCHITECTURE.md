# Architecture: extension points

## Agent-Factory process boundaries

### Capability package release chain

`agenten.agent_factory.capability_factory_entrypoint` is the Package-C
composition root for one immutable `TO_BE_BUILT.md` correlation. It uses the
real input parser/compiler, `FactoryCoordinator`, package validator, Gateway
repository/catalog, terminal policy, execution contracts, and Minibook
projector. External creation, provider execution, Gateway HTTP, Runtime HTTP,
and Minibook HTTP effects remain injected ports. The deterministic integration
test scripts only those external ports; it does not claim provider execution.

`ready_to_use` has one write path: after the promotion block, the composition
derives the candidate decision in memory and calls
`GatewayStore.publish_capability_release` once. That transaction owns the
terminal decision, published package, and full frozen
`CapabilityCatalogRecord`; the composition must read all three back before
execution. It never writes a READY terminal through the non-release terminal
API. A compatible catalog hit reuses that complete record, skips Forge,
release E2E, sandbox validation, and republication, and runs the new
correlation through command admission, capability grant, fenced execution
claim, provider result, and `CapabilityExecutionRequest` recording.

The entrypoint binds correlation ID, invocation Factory job ID, subject version,
exact input SHA-256, `occurred_at`, and immutable deadline in an append-only
checkpoint before the first external effect. It registers that invocation
idempotently before catalog resolution. Replays use the same
creation/run/command IDs. Changed input bytes or timing fail closed instead of
creating a replacement job. Captain's Evidence Issuer is the
only port permitted to return `captain.capability-release-evidence.v1`; the
composition rechecks every job, creation, candidate, run-order, producer, and
digest binding before policy evaluation. Recovery is distinct from the three
normal successes and never counts toward that streak.
Restart tests reconstruct the file-checkpoint, repository/catalog, entrypoint,
and runtime composition adapters while retaining the scripted authoritative
service state. Crashes after creation submission, release evidence, committed
atomic publication, runtime claim, durable provider effect, runtime result,
and committed capability execution expose exactly one provider effect after
recovery. Provider adapters must declare durable idempotency and persist a
`ProviderEffectReceipt` keyed by the stable command/effect IDs before returning
control. Captain looks that receipt up before any re-effect and verifies the
provider operation ID, request/result digests, result status, explicit
idempotency guarantee, and the effect-origin claim ID/fence/digest. An
unexpired claim returns typed `retry_pending`; after
expiry the Gateway issues a higher fence without calling the provider again.
The original `AgentRuntimeResult` is then stored byte-stably with its original
event ID, timestamp, evidence, and business output. A separate Captain-owned
`RuntimeResultRecoveryObservation` is recorded inside the recovery lease. It
binds the original result and durable provider receipt by digest/effect ID,
pins the receipt's historical origin claim independently of any intervening
expired recovery claims, links the active recovery fence, and has its own event
ID. The Gateway locks and validates both append-only claim blocks before atomically
storing the result, observation, and completed recovery claim. No claim
credential or provider secret enters a checkpoint, receipt, or summary.
Deadline checks run
before each later mutation and before requesting lifecycle evidence, so an
expired post-publication run cannot begin runtime work and an expired
post-execution run cannot mutate Minibook.

Forge archives are independently validated in a disposable Captain-owned
Docker sandbox. The production runner accepts only a digest-pinned
`captain-*` image already present locally (`--pull never`), then inspects the
created container before execution. It requires a non-root user, no network,
a read-only root filesystem and exact read-only workspace bind, bounded tmpfs,
memory/PID limits, all capabilities dropped, `no-new-privileges`, and an init
process for exact-tree cancellation. Attestations are derived from `docker
inspect`; an unavailable image or any inspection mismatch is an isolation
failure, never synthetic evidence. The package tree is hashed again before
container creation, and Python bytecode/cache output is directed away from the
host workspace.
The isolated interpreter explicitly inserts `/workspace` into `sys.path`
before module discovery, and Docker inspection must report the exact
`rw,noexec,nosuid,size=67108864` tmpfs. The Docker CLI adapter applies a
bounded timeout to every command and verifies the exit status of process-tree
termination; the package validator additionally owns its outer timeout and
exact-identity cancellation contract. An actual Docker import/pytest smoke
test is live-only, has its own outer bound, and remains blocked when
the configured digest is not already local.

CLI configuration resolves input, artifact, and checkpoint paths under the
workspace; accepts credential-free HTTP service URLs; and reads bearer/API
credentials only from environment aliases. Its evidence manifest contains
only typed IDs and digests, is canonical JSON named by its own SHA-256, and is
written under the gitignored `artifacts/capability-factory/` directory.
The module is executable with `python -m`. Preflight first parses and compiles
the selected input and verifies a v2 static adapter manifest without importing
or instantiating its module. The manifest names one Python module path under
the workspace root, its SHA-256, and one factory symbol; preflight reads and
hashes the source bytes and proves the top-level symbol through Python AST.
Only the full run loads that attested local module and factory, writes the evidence manifest, and
prints redacted targets, timings, IDs, and digests. Static manifest, path,
source-digest, syntax, and factory-symbol failures occur before database
attestation or service startup. Runtime dependency resolution, top-level module
execution, and factory instantiation occur only in the full invocation after
database attestation and service health checks; they fail closed before any
factory effect, but are not part of the side-effect-free preflight claim.

The provider-backed gate is
`scripts/run-capability-factory-live.ps1`. It validates configuration first,
requires loopback MariaDB database exactly `captain_test`, and only then may
start a dedicated Gateway process bound to that DSN. Runtime and Minibook must
also pass health checks before the full, mutating invocation. It never starts
or adopts
an external workflow service. It must observe one recovery, three distinct
normal successes, Gateway terminal/catalog/execution records, an ordered feed,
and a Minibook rebuild before a `ready_to_use` claim. The current production
gate remains blocked: the Runtime has no production Hermes/Codex/artifact port
bundle, no production capability-factory creation/Evidence-Issuer HTTP adapter
bundle is installed, and no reviewed digest-pinned capability-sandbox image is
configured. Deterministic success does not satisfy those live prerequisites.
Separately, an explicitly selected `live`/`db_mutating` MariaDB acceptance
proof accepts only the process-exported `TEST_MARIADB_DSN`; ordinary non-live
tests neither discover `.env` values nor clear a database. The live proof
executes production
`GatewayStore` methods against `captain_test`: an injected mid-publication
crash rolls the transaction back, a reconstructed store replays the single
publication, and production command/grant/claim/result/execution writes replay
from their authoritative readbacks.

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

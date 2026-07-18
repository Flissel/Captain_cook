# Architecture: extension points

## Agent-Factory process boundaries

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
paginated `captain.minibook-projection.v1` feed and uses only Minibook's public
HTTP projects, posts, comments, and search routes. Captain production modules
do not import `minibook.src`, its SQLite models, Hermes, or the Forge pipeline.

The projection envelope is idempotent by event ID and monotonic by subject
version. A local SQLite cursor stores only event/post identity, subject heads,
feed position, and quarantine reasons. It does not store event bodies. Before
creating a post the projector searches for its event tag and compares a
deterministic content hash, so replay converges even if a process stopped after
the remote write and before the local cursor commit.

The event payload is a strict public allow-list: batch identity/version, title,
status, display assignee, artifact digest, and short evidence summary. Unsafe
keys and absolute filesystem paths fail closed. Rebuild is dry-run by default;
`--apply` repairs missing or modified projection posts and retires only marked
duplicates/orphans, leaving unrelated Minibook content untouched.

Minibook starts independently with `python run.py`. Its health gate requires no
Captain, Hermes, Codex, Docker, Forge, or n8n process. The separate live replay
gate starts that package command, reads a redacted event from a public HTTP
feed, restarts the projector, mutates and rebuilds the view, and requires zero
skips.

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

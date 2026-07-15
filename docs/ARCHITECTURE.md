# Architecture: extension points

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

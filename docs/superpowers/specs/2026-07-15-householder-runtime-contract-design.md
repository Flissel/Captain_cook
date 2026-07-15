# Householder Runtime Contract Design

## Decision

Portable role prompts in `agents/household/` are the human-readable source of
truth. The in-memory pipeline receives a typed, constrained manifest for each
role and only creates workers through an injected factory seam.

## Scope of `feat/householder-runtime-contract`

- Validate each known Markdown role's frontmatter and map it to exactly one
  agent type and capability tag.
- Expose a `HouseholderExecutor` port and a JSON-safe `HouseholderReport`.
- Add a typed `WorkerFactory` injection point to `build_pipeline`.
- Fail boot if a worker duplicates an agent type or shadows another worker's
  capability tag.

This branch introduces no live model calls, MCP calls, browser activity,
filesystem writes by workers, Docker services, or deployments.

## Runtime contract

| Role ID | Agent type | Capability tag | Prompt source |
| --- | --- | --- | --- |
| `architect` | `householder_architect` | `architecture_review` | `agents/household/architect.md` |
| `ledger-steward` | `householder_ledger_steward` | `ledger_review` | `agents/household/ledger-steward.md` |
| `delivery-builder` | `householder_delivery_builder` | `delivery_plan` | `agents/household/delivery-builder.md` |
| `quality-warden` | `householder_quality_warden` | `quality_review` | `agents/household/quality-warden.md` |

Each executor result is a JSON-safe object with `role`, `decision`,
`artifacts`, `evidence`, and `limitations`. The deterministic executor must
declare that it did not invoke an LLM, MCP server, browser, or deployment.

## Factory ownership

`build_pipeline` owns the event bus, tool registry, heartbeat interval, and
ledger-backed description resolver. A factory receives those dependencies and
returns one unsubscribed `WorkerAgent`; the pipeline then registers its tags,
subscribes it, and validates the final fleet at boot.

This keeps a future MCP-backed executor behind the same worker/ledger contract
and prevents a role file from silently granting itself a new routed capability.

## Acceptance

```powershell
python -m pytest tests/test_householder_roles.py tests/test_worker_factories.py tests/test_e2e_smoke.py -q
```

The next branch, `feat/householder-runtime`, owns the concrete
`HouseholderWorker`, deterministic four-role run, and demo integration.

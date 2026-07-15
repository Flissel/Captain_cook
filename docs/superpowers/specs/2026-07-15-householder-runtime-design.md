# Householder Runtime Design

## Decision

The Devpost demo will route four atomic work items through the existing
event-driven pipeline to four deterministic `HouseholderWorker` instances.
The roles use the manifest and executor port from
`feat/householder-runtime-contract`; they do not require an API key or use an
external tool.

## Data flow

```text
deterministic decomposer
  -> constitution gate
  -> capability coordinator
  -> HouseholderWorker(role-specific capability)
  -> HouseholderReport
  -> SubproblemCompleted
  -> ledger recorder
  -> JSON demo evidence
```

The four report fields are `role`, `decision`, `artifacts`, `evidence`, and
`limitations`. Reports are JSON-safe and tell a reviewer exactly what did not
run: no LLM, MCP server, browser, or deployment.

## Error behavior

- An executor may raise `HouseholderExecutionError` with an explicit retry
  decision; the worker preserves it as `WorkerExecutionError`.
- Any other executor exception becomes a retriable worker error and enters the
  existing supervisor/retry lifecycle.
- Pipeline boot fails before accepting work if a worker duplicates an agent
  type or shadows an already-owned capability tag.

## Acceptance

```powershell
python -m pytest tests/test_householder_runtime.py tests/test_demo.py tests/test_main_cli.py -q
python main.py demo --output artifacts/demo-run.json
```

The next runtime expansion is not a live executor. It is the independent
`feat/ledger-gateway` branch, which requires a MariaDB-backed, sole-writer
gateway contract and its own integration proof.

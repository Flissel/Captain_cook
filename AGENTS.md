# Captain Cook agent guide

## Purpose

Captain Cook is an event-driven multi-agent delivery runtime. The root Python
product decomposes work, applies constitutional checks, routes constrained
workers, supervises retries, and records lifecycle evidence in an append-only
ledger. `minibook/` and the `hermes-agent/` submodule are adjacent products;
do not treat them as ordinary root-runtime packages.

## Read before changing code

1. Read `docs/WORKSTREAMS.md` for branch ownership and dependency order.
2. Read `docs/ARCHITECTURE.md` for current extension points.
3. Check the relevant spec and plan under `docs/superpowers/`.
4. Run `git status --short --branch` and `git worktree list --porcelain`.
5. Preserve all changes you did not create. Several feature branches are
   developed concurrently in `.worktrees/`.

The prioritized architecture and branch-safety backlog is
`docs/superpowers/plans/2026-07-15-architecture-gap-todos.md`.

## Architecture boundaries

- `agenten/events/`: shared event contracts; keep free of orchestration and
  infrastructure dependencies.
- `agenten/runtime/`: event-bus ports and AutoGen adapter/bootstrap.
- `agenten/decomposition/` and `agenten/constitution/`: domain decisions.
- `agenten/spawning/`, `agenten/workers/`, `agenten/supervision/`: routing and
  execution lifecycle.
- `agenten/ledger_bridge/`: sole-writer recording, queries, recovery, and stage
  transitions.
- `agenten/orchestration/pipeline.py`: composition root; do not add new domain
  behavior here when it belongs behind an existing port.
- `blockchain/`: ledger model and storage backends. Avoid new imports from
  `blockchain` into concrete agent implementations.
- `agenten/workflows/`: legacy-compatible AgentChat workflows, separate from
  the event-driven supply-chain runtime.
- `chats/project_maker.py`: compatibility entry point; canonical workflow code
  lives under `agenten/workflows/`.
- `minibook/`: independently runnable backend/frontend application.
- `hermes-agent/`: Git submodule; never absorb its changes into the parent repo
  accidentally.

Keep interfaces typed, inject executors/storage/event buses, and preserve the
deterministic offline path. Live LLM, MCP, browser, Docker, or deployment work
must not be claimed from mocked evidence.

## Development workflow

- Use Python 3.11 and the project `.venv`. Install `requirements-dev.txt` for
  development and testing; runtime-only environments install `requirements.txt`.
- Add a failing acceptance test before behavioral changes.
- Prefer focused tests while iterating, then run the complete gate.
- Keep commits narrow and Conventional Commit formatted.
- Work on the branch that owns the contract. Do not commit directly to
  `main`; do not create, delete, merge, rebase, or push branches unless the
  current task authorizes it.
- Before integrating concurrent branches, simulate the intended merge and
  inspect overlap in `README.md`, `.env.example`, `requirements.txt`, and
  `main.py`.
- Record newly discovered architectural work as checkboxes in a dated file
  under `docs/superpowers/plans/`.

## Verification commands

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/verify_submission.py
.\.venv\Scripts\python.exe main.py demo --output artifacts/demo-run.json
```

For import or architecture work, also run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
.\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config
```

Do not rewrite `artifacts/demo-run.json` unless the task intentionally updates
demo evidence. Report skipped tests and dependency warnings separately from
failures.

## Secrets and local services

- Secrets belong only in gitignored `.env` files or user-level tool config.
- Never print or commit `.env`, API keys, database passwords, MCP tokens, or
  Hermes/Minibook credentials.
- Captain-owned services are Mailpit and MariaDB. VibeMind owns the external
  n8n deployment and its volumes.
- Never run `docker compose down -v`, delete Docker volumes, or migrate/adopt
  VibeMind n8n volumes without explicit user approval.
- Validate Compose configuration before starting services. Do not start live
  infrastructure merely to run unit tests.

## Known active concerns

- Branch aliases and integration order need cleanup; follow the architecture
  gap plan instead of guessing branch ownership.
- `agenten/ledger_bridge/recorder.py` and
  `agenten/orchestration/pipeline.py` are concentration hotspots.
- Permanently unroutable work lacks a durable dead-letter event.
- The AutoGen event-bus subscription boundary is intentionally incomplete.
- The architecture document does not yet describe the full event-driven path;
  code and tests are the stronger evidence where they disagree with prose.

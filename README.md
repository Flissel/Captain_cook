# Captain Cook

**Captain Cook is an auditable agent-work orchestrator:** it decomposes an engineering problem, gates proposed work against a constitution, routes accepted tasks to workers, and records the lifecycle in a local ledger.

This repository contains a working, offline vertical slice for the OpenAI Build Week **Developer Tools** track. It is intentionally honest about the boundary: the deterministic orchestration demo works today; the larger Captain → Hermes → Codex delivery fleet is a documented roadmap, not a claim about the current runtime.

## See it work in 90 seconds

```text
problem
  │
  ▼
decomposer → constitution gate → capability coordinator → echo worker
  │                                                       │
  └─────────────── append-only lifecycle ledger ◀─────────┘
```

Run the demo without an API key, network access, Docker, or a browser:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py demo --output artifacts/demo-run.json
```

Expected output:

```text
Demo complete: 2 subproblems reached done
```

The command writes [artifacts/demo-run.json](artifacts/demo-run.json), a compact evidence artifact containing the generated problem ID, terminal counts, and every ledger block in the run. See [docs/DEMO.md](docs/DEMO.md) for the schema and inspection steps.

## What is working now

- Deterministic problem decomposition into atomic, capability-tagged tasks.
- Constitution gatekeeping before work reaches a worker.
- Capability-based assignment, worker heartbeats, retries, circuit breaking, lease reaping, and recovery primitives.
- A sole-writer ledger recorder and state-machine transitions for an auditable run.
- An offline CLI demo and committed run evidence.

## Roadmap boundary

The submission demo does **not** yet include a FastAPI/MariaDB ledger gateway, Hermes workers that drive Codex CLI, n8n deployment, Mailpit validation, or Minibook mirroring. Those integrations are designed in [the delivery-fleet specification](docs/superpowers/specs/2026-07-15-hackathon-pipeline-design.md) and deliberately kept separate from claims about the runnable demo.

## Test it

```powershell
python -m pytest -q
python scripts/verify_submission.py
```

The first command runs the engineering regression suite. The second verifies that the judge-facing documentation and committed evidence artifact are present and well-formed. See [docs/MCP_SETUP.md](docs/MCP_SETUP.md) for the development-time Playwright, Context7, and n8n MCP boundaries.

## How Codex and GPT-5.6 fit

Codex is used to build, test, and document the Devpost-ready vertical slice; the implementation history is recorded in this repository's Devpost feature branch and [docs/codex-sessions.md](docs/codex-sessions.md) records the primary submission session ID once captured. The LLM-backed production path is intentionally separate from the offline demo; its target model is configured as GPT-5.6 before the Devpost run. The video must show the working demo and explain both uses, as scripted in [docs/VIDEO_SCRIPT.md](docs/VIDEO_SCRIPT.md).

## Platform and layout

Windows 11 is the supported and tested platform. Python 3.11 is required. The offline demo does not need external services; the future delivery fleet will require Docker and third-party service credentials.

```text
agenten/       event-driven orchestration, agents, workers, and demo adapter
blockchain/    hash-chained ledger and storage abstractions
tests/         regression, integration, and CLI tests
artifacts/     committed judge-inspectable demo evidence
docs/          architecture, demo, video, and submission guidance
minibook/      vendored third-party AGPL-3.0 project
hermes-agent/  third-party MIT-licensed Git submodule
```

## Licensing and third-party code

The project-root license must be selected by the repository owner before public publication. Third-party components and their licenses are listed in [docs/THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md); do not treat the third-party licenses as the license for Captain Cook itself.

## Submission checklist

The remaining account-owned actions—repository publication, Devpost form, video upload, and `/feedback` session ID—are tracked in [docs/DEVPOST_CHECKLIST.md](docs/DEVPOST_CHECKLIST.md).

# Captain Cook

**Captain Cook is an auditable agent-work orchestrator:** it decomposes an engineering problem, gates proposed work against a constitution, routes accepted tasks to workers, and records the lifecycle in a local ledger.

This repository contains a working, offline vertical slice for the OpenAI Build Week **Developer Tools** track. It is intentionally honest about the boundary: the deterministic orchestration demo works today; the larger Captain → Hermes → Codex delivery fleet is a documented roadmap, not a claim about the current runtime.

## Einfache Einrichtung unter Windows 11

Öffne PowerShell 7 im Projektordner und starte genau einen Befehl:

```powershell
.\setup.ps1
```

Der Assistent prüft Windows, Git, Python, Node.js und Docker. Falls etwas
fehlt, erklärt er den Grund und fragt vor jeder Installation nach. Danach
richtet er Captain Cook, Hermes Agent, Minibook, Mailpit, MariaDB und n8n ein,
startet die lokalen Dienste und prüft ihre öffentlichen Schnittstellen. Ein
abgebrochener Lauf wird gespeichert; derselbe Befehl setzt beim ersten
unvollständigen Schritt fort.

API-Keys werden nur abgefragt, wenn die jeweilige optionale Integration sie
benötigt. Passwörter werden verborgen eingegeben oder sicher erzeugt und nur
in der von Git ignorierten `.env` beziehungsweise im lokalen Hermes-Profil
gespeichert. Sie erscheinen nicht in den Setup-Logs.

Nach der Einrichtung verwendest du:

```powershell
.\start.ps1
.\status.ps1
.\status.ps1 -Detailed
.\repair.ps1
.\stop.ps1
```

- Minibook: `http://localhost:3457`
- Mailpit: `http://localhost:8025`
- n8n: standardmäßig die bestehende VibeMind-Instanz unter
  `http://localhost:15678`; die Adresse steht in `.env`.

`start.ps1` und `stop.ps1` steuern ausschließlich Captain-eigene Prozesse und
Container. Eine übernommene n8n-Instanz wird weder gestartet noch gestoppt.
Docker-Volumes werden durch die Lifecycle-Befehle nie gelöscht.

### Wenn etwas nicht funktioniert

Starte zunächst:

```powershell
.\status.ps1 -Detailed
.\repair.ps1
```

`Missing` bedeutet, dass ein benötigtes Programm fehlt. `Configure` weist auf
eine fehlende oder abgelehnte lokale Einstellung hin. `Retry` bedeutet, dass
ein Dienst noch nicht erreichbar ist und erneut geprüft werden kann. Logs und
Fortschritt liegen in `.captain-cook/`; der Ordner wird nicht committed und
enthält keine Zugangsdaten. Das Setup beendet keine fremden Prozesse bei einem
Portkonflikt, sondern zeigt den belegten Port zur manuellen Klärung an.

Fortgeschrittene Nutzer können die bisherigen manuellen Schritte weiterhin
verwenden. Für neue Nutzer ist `.\setup.ps1` der unterstützte Einstieg.

## See it work in 90 seconds

```text
problem
  │
  ▼
decomposer → constitution gate → capability coordinator → householder fleet
  │                                                            │
  └──────────────── append-only lifecycle ledger ◀────────────┘
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
Demo complete: 4 subproblems reached done
```

The command writes [artifacts/demo-run.json](artifacts/demo-run.json), a compact evidence artifact containing the generated problem ID, terminal counts, and every ledger block in the run. See [docs/DEMO.md](docs/DEMO.md) for the schema and inspection steps.

## What is working now

- Deterministic problem decomposition into atomic, capability-tagged tasks.
- Constitution gatekeeping before work reaches a worker.
- Capability-based assignment, worker heartbeats, retries, circuit breaking, lease reaping, and recovery primitives.
- Four constrained Householder workers (Architect, Ledger Steward, Delivery Builder, and Quality Warden) that emit structured, explicitly offline audit reports.
- A sole-writer ledger recorder and state-machine transitions for an auditable run.
- An offline CLI demo and committed run evidence.
- A loopback-bound Captain Gateway with MariaDB-backed, append-only delivery
  evidence, short-lived claims and a start-time recovery pass. `start.ps1`
  starts it after MariaDB is healthy; `status.ps1 -Detailed` includes its
  health endpoint.

## Run the standalone Captain planner

The Captain can turn a UTF-8 project description into executor-neutral,
versioned work contracts without starting Hermes, n8n, or another delivery
runtime. Configure the capability vocabulary explicitly; the model may group
and enrich work but cannot choose the target or invent capability tags.

```powershell
python -m agenten.planning.cli docs/superpowers/specs/2026-07-15-hackathon-pipeline-design.md `
  --capability planning `
  --capability delivery `
  --target external `
  --output artifacts/captain-release
```

The command uses `CAPTAIN_MODEL` (default `gpt-5.6`) and `OPENAI_API_KEY`.
It writes build-visible contracts to `batches/<batch-id>.json` and hidden
evaluation inputs to `holdouts/<batch-id>.json`. Releases are idempotent:
re-running identical output succeeds, while conflicting content for an
existing batch id fails instead of overwriting the contract.

Captain planning enforces these rules deterministically after every model
response:

- every decomposed subtask appears in exactly one batch;
- batch ids and dependency references are valid;
- the dependency graph is acyclic and released in topological order;
- acceptance criteria use a closed, observable assertion vocabulary;
- golden examples and holdout cases remain separate.

External executors integrate by implementing the small `BatchReleaseClient`
protocol in `agenten/planning/captain_pipeline.py`. The Captain repository does
not implement or operate those external systems.

## Evaluate the AgentFarm input as plans

The evaluation CLI reads immutable Markdown, asks the bounded AutoGen Society
for a component inventory and QA-reviewed plans, and writes Captain-owned
evidence under `artifacts/evaluations/`. It is planning-only: listed acceptance
tests are not executed and no Codex, n8n, Hermes, Minibook, browser, or delivery
capability is available.

```powershell
python -m agenten.evaluation.cli $env:AGENTFARM_INPUT_PATH `
  --source-reference agentfarm/input.md `
  --max-components 1 `
  --max-rounds 3 `
  --max-calls 8
```

The command prints only a redacted JSON summary with a relative artifact
reference. Source paths, source text, prompts, provider responses, and API keys
are not written to that summary or the evaluation report. Exit code `0` means
the Captain manifest is `accepted`; `2` means the run produced a durable
`partial` or `failed` manifest; configuration or unrecoverable setup errors use
exit code `1`. Provider cost is reported as unavailable when the client cannot
provide a trustworthy value.

The real-model smoke gate is opt-in and fixes its own stricter budget. Set
`AGENTFARM_INPUT_PATH` and `OPENAI_API_KEY` only in the local process
environment, then run:

```powershell
python -m pytest -q -m live tests/live/test_agentfarm_input_evaluation_live.py
```

That gate verifies the approved source digest before any provider call and
enforces one component, one Planner/QA round, and at most four raw model calls.
A missing path or API key skips the opt-in gate. A configured unreadable or
digest-mismatched source fails the gate before provider construction. It does
not contact or modify n8n or any other local service.

## Gateway recovery at startup

`start.ps1` creates missing local Gateway credentials in the gitignored `.env`,
starts the Gateway after MariaDB, waits for `/healthz`, and executes one
Captain-owned recovery pass before starting dependent local processes. The
same bounded pass can be run independently:

```powershell
python main.py recover-gateway
```

It reports only durable `recovered_batch_ids` and sessions that remain
`deferred_batch_ids`. A deferred batch has an active Codex session without
terminal process evidence; Captain deliberately does not requeue it, because
doing so could run an external provider twice. A host-local worker-recovery
director is required to prove that process terminal before it can be requeued.

## Runtime boundary

The offline demo remains a deliberately separate evidence path. The live
delivery path provides the FastAPI/MariaDB Gateway, Captain-fenced Codex
sessions, constrained n8n MCP leases, Mailpit and Minibook integrations, and
the bounded live LLM planning evaluation. Full automatic recovery of an active
Codex worker still requires the host-local process-evidence director described
above; it is not claimed as completed.

## Test it

```powershell
python -m pytest -q
python scripts/verify_submission.py
```

The first command runs the engineering regression suite. The second verifies that the judge-facing documentation and committed evidence artifact are present and well-formed. See [docs/MCP_SETUP.md](docs/MCP_SETUP.md) for the development-time Playwright, Context7, and n8n MCP boundaries.

## Local delivery services

Captain Cook reuses the existing VibeMind n8n instance and owns only Mailpit
and MariaDB. This keeps VibeMind's workflows, credentials, encryption key, and
`voice_vibemind-n8n-data` volume under the VibeMind project's control.

Prerequisites are Docker Desktop and the existing VibeMind checkout at
`C:\Users\User\Desktop\Vibemind_V1\vibemind-os\voice`. Copy the delivery
values from `.env.example` into the gitignored `.env` and replace both MariaDB
password placeholders with different random values. Then start and verify the
complete local stack:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_delivery_stack.ps1
```

| Service | Local endpoint | Ownership |
| --- | --- | --- |
| n8n | http://localhost:15678 | Existing VibeMind Compose project |
| Mailpit | http://localhost:8025 (SMTP `localhost:1025`) | Captain Cook |
| MariaDB | `localhost:3306`, database `ledger` | Captain Cook |

Run the non-destructive checks again at any time with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_delivery_stack.ps1
```

Stop Captain Cook's services with `docker compose down`. Do not run
`docker compose down -v`: it deletes the Captain ledger volume. Captain Cook's
scripts never delete or adopt either existing n8n volume.

### Isolated Captain n8n builder

The optional Captain builder is a separate Compose project on
`http://127.0.0.1:5679`. It has its own PostgreSQL database, encryption key,
API key, and named volumes. It does not replace the default external n8n mode.
VibeMind remains untouched: these commands do not contact its API or inspect,
start, stop, restart, mount, or alter its container, workflows, or volumes.

Run the lifecycle in order from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/captain-n8n.ps1 -Action init
powershell -ExecutionPolicy Bypass -File scripts/captain-n8n.ps1 -Action start
powershell -ExecutionPolicy Bypass -File scripts/captain-n8n.ps1 -Action bootstrap
powershell -ExecutionPolicy Bypass -File scripts/captain-n8n.ps1 -Action status
powershell -ExecutionPolicy Bypass -File scripts/captain-n8n.ps1 -Action stop
```

`init` generates missing local secrets in the gitignored
`.env.captain-n8n`. `bootstrap` creates or authenticates the local owner
`captain@local.test`, creates or recovers one labelled API key through n8n's
supported API, and stores that key only in the same environment file. It does
not edit n8n database rows. Verify the running builder without displaying
credentials or workflow content:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_captain_n8n.ps1
```

### Hermes runtime readiness

Captain's pinned Hermes submodule can be checked without starting Docker or
contacting n8n:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_hermes_readiness.ps1
```

The verifier fails closed when Hermes is uninitialized, differs from the
parent gitlink, or has local changes. Its redacted report lists only the pinned
commit, required Captain-planner/MCP entrypoints, focused-test status, and the
lease-scoped `n8n-mcp` server identity.

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

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
- n8n: standardmäßig `http://localhost:5678`; bei einer übernommenen externen
  Instanz steht die Adresse in `.env`.

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

## Roadmap boundary

The submission demo does **not** yet include a FastAPI/MariaDB ledger gateway, Hermes workers that drive Codex CLI, n8n deployment, Mailpit validation, Minibook mirroring, or a live LLM/MCP-backed Householder executor. Those integrations are designed in [the delivery-fleet specification](docs/superpowers/specs/2026-07-15-hackathon-pipeline-design.md) and deliberately kept separate from claims about the runnable demo.

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

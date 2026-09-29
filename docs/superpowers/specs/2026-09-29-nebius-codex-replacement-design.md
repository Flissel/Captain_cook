# Nebius/Nemotron + Claude-Code-in-Sandbox: Codex-Ablösung — Design

Datum: 2026-09-29 · Branch: `feat/nebius-claude-sandbox`
(Basis `claude/householder-llm-executor-v1` + Merge `codex/live-demo-integration`)

## 1. Ziel und Rahmen

Captain Cook wird für den *Nebius x NVIDIA Global AI Hackathon*
(Coding and Agentic Engineering Track, Einreichung bis 2026-10-30 10:00 PT)
auf offene Infrastruktur umgestellt:

- Jeder Modellaufruf läuft über **Nebius Token Factory** mit einem
  **NVIDIA-Nemotron**-Modell.
- **Codex CLI wird vollständig ersetzt** durch **Claude Code CLI**, die in einer
  **Token-Factory-Sandbox (ConTree-microVM)** läuft und ihre Modellaufrufe über
  einen Captain-eigenen Relay an Nemotron schickt.
- Budget-Metering, Gateway-Evidenz und Ledger bleiben die Governance-Schicht;
  jeder Nemotron-Token ist budgetiert und belegt.

Nicht-Ziele: Umbau von Minibook, des `hermes-agent`-Submoduls oder der
Portal-/Mini-PC-Deployments; Rückwärts-*Schreib*-Kompatibilität zu Codex.

## 2. Ist-Zustand (Workflow-Analyse 2026-09-29)

- **Pipeline:** `main.py demo` → `build_pipeline` → Decomposer → Constitution
  (Regeln + LLM-Judge) → Ledger-Recorder → Spawn-Coordinator → Householder-Worker
  → Supervisor. Householder nutzen überall `DeterministicHouseholderExecutor`;
  `LlmHouseholderExecutor` existiert, ist aber nirgends verdrahtet.
- **Hermes-Fleet:** vier geleaste Factory-Rollen (architect, tool_integrator,
  real_case_tester, quality_warden); jede Dispatch ist ein einmaliger
  `hermes -z`-Prozess, der attestiert. Modellwahl über
  `CAPTAIN_HERMES_PROVIDER/MODEL/BASE_URL`.
- **Codex** läuft in drei Pfaden: Factory-Build (`codex_build_execution.py`),
  Runtime (`runtime_codex.py`, Operationen `codex.*`), Delivery
  (`delivery/codex_runs.py`); dazu Supervisor/Policy/Events unter
  `agenten/execution/` und `scripts/codex-session.ps1`.
- **Ledger:** SHA-256-Hash-Kette in MariaDB `blocks`, einziger Schreiber ist das
  Gateway (append-only). Gehasht werden `index, block_type, data,
  previous_hash, parent_index`; `status/children/metadata` nicht. `verify_chain`
  existiert, wird produktiv nicht aufgerufen.
- **Provider-Proxy:** budgetgebunden, aber hart auf `api.openai.com` +
  Responses-API + `gpt-5.6-terra`; `provider_proxy_url` per Pattern auf
  `http://127.0.0.1:<port>/v1` gepinnt.
- **Betrieb:** Gateway, Runtime, Minibook binden hart an `127.0.0.1`; Starter
  sind Windows/PowerShell-only.

## 3. Architektur

```
                        ┌──────── gemeinsamer Coding-Agent-Kern ─────────────────┐
 Factory-Build   ─┐     │ AgentRunRequest v2 (workspace, brief, budget, timeout)  │
 Runtime agent.*  ├──►  │ ClaudeSandboxRunner                                     │
 Delivery        ─┘     │  1 Workspace → ConTree-Image (Checkpoint)              │
                        │  2 claude -p --output-format stream-json [--resume]    │
                        │       ANTHROPIC_BASE_URL = gehosteter Captain-Proxy     │
                        │       ANTHROPIC_AUTH_TOKEN = kurzlebiges Run-Token      │
                        │  3 stdout-JSONL → ClaudeStreamEvents → AgentEvent v2    │
                        │  4 Outputs + Images → AgentRunReceipt v2 (CAS)          │
                        └──────────────────────────────┬──────────────────────────┘
                                                       ▼
 Captain-Provider-Proxy (gehostet, HTTPS)   /v1/messages → Übersetzung → Token Factory (Nemotron)
                                            Preiskarte "nebius", Settlement → Gateway → Ledger
```

Alle anderen LLM-Nutzer (Planner, Judge, Decompose, Captain, Hermes,
LLM-Householder, Evaluation, Benchmark) sprechen direkt die OpenAI-kompatible
Token-Factory-API mit Nemotron an (M0).

## 4. Relay im Provider-Proxy

Bestehende Budget-Logik (`ProviderBudgetProxy`, Gateway-Autorität vor jedem Call,
Settlement danach) bleibt. Neu:

- **Eingang** `POST /v1/messages` (Query wie `?beta=true` wird verworfen) und
  `POST /v1/messages/count_tokens` (konservative lokale Schätzung, als geschätzt
  markiert — Nebius hat keinen Count-Endpunkt).
- **Übersetzung** `agenten/provider_proxy/anthropic_bridge.py`, reine Funktionen:
  - Request: `system`-Blöcke → System-Message; `tool_use` →
    `assistant.tool_calls`; `tool_result` → `role:"tool"`;
    `tools[].input_schema` → `function.parameters`. Verworfen: `output_config`,
    `thinking`, `cache_control`, `metadata`, Betas. Tool-Namen > 64 Zeichen
    werden gehasht gekürzt und pro Request zurückgemappt. Bilder → Ablehnung.
  - Stream: Chat-SSE → Anthropic-SSE (`message_start`, `content_block_start`,
    `text_delta`/`input_json_delta`, `content_block_stop`, `message_delta` mit
    `stop_reason`/`usage`, `message_stop`). Fragmentierte Tool-Argumente werden
    pro Index akkumuliert. `finish_reason`: `tool_calls→tool_use`,
    `length→max_tokens`, `stop→end_turn`. `reasoning_content` wird nicht
    weitergereicht, aber gezählt.
- **Transport** `NebiusChatCompletionsHTTPClient`, Basis-URL gepinnt auf
  `https://api.tokenfactory.nebius.com/v1`, `stream_options.include_usage=true`.
  `NEBIUS_API_KEY` existiert nur im Proxy.
- **Verträge v2:** `ProviderPriceCardV2` (`provider:"nebius"`, Nemotron-Allowlist
  mit Preisen); `RuntimeProviderPolicyV2` (`maximum_provider_calls` ≈ 200, da
  agentische Läufe viele Calls erzeugen; Kostendeckel pro Lauf bleibt).
  Modell-Mapping: `ANTHROPIC_MODEL` → Nemotron Super/Ultra (nach Spike),
  `ANTHROPIC_SMALL_FAST_MODEL` → Nemotron Nano; unbekannte Modelle → Ablehnung.
- **Auth:** pro Lauf ein kurzlebiges Run-Token, ausgestellt über den
  getrennten Runtime-Kanal `POST /v1/captain/run-tokens`
  (`runtime_finalize_token`), gebunden an Budget-Kontext + Ablauf; Proxy
  speichert nur den Hash. `RuntimeProviderProxyBinding` erlaubt HTTPS-Hosts aus
  einer Allowlist statt nur `127.0.0.1`.
- **Fehler:** Anthropic-Fehlerformat. Budget erschöpft → **400
  `invalid_request_error`** (nicht 429/529, sonst endlose Retries).
  Upstream-5xx → `api_error` (Retry erlaubt), 4xx durchgereicht. Inaktive
  Gateway-Autorität → Ablehnung vor dem Upstream-Call.

## 5. ClaudeSandboxRunner

Ein Lauf = ConTree-Operationen mit je einem Checkpoint:

1. **PREPARE** — Workspace-Tar (git archive + Brief) hochladen, entpacken → Image W.
2. **RUN** — auf W:
   `claude -p --output-format stream-json --verbose --model <mapped>
   --max-turns N --dangerously-skip-permissions
   --allowedTools "Bash,Read,Edit,Write,Glob,Grep" --setting-sources ""
   --strict-mcp-config` mit Brief über stdin → Image R (enthält `~/.claude`-Session).
   Die microVM ist die Sicherheitsgrenze; deshalb ist Permission-Skip vertretbar.
3. **COLLECT** — `git status`/`diff`, Output-Prüfung, Bündelung → Manifest (+Outputs).
4. **RESUME** — erneuter RUN auf R mit `--resume <session_id>` → R'.

- Basis-Image B: einmal gebaut (node + `@anthropic-ai/claude-code@<pin>` +
  python + git), per Image-ID/Digest gepinnt.
- Environment-Allowlist: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`.
  Nie `NEBIUS_API_KEY`/`ANTHROPIC_API_KEY`.
- `agent.status`/`agent.heartbeat` fragen den Operation-Status ab;
  `agent.cancel` per ConTree-Cancel (falls vorhanden) sonst Operation-Timeout +
  Captain-Wall-Clock.
- **Events** (`claude_stream.py`): `system/init`, `assistant` (tool_use),
  `user` (tool_result), `result` (`success`/`error_max_turns`/
  `error_during_execution`, `num_turns`, `usage`) → `AgentEvent v2`
  (`started, tool_call, tool_result, turn_completed, completed, failed`).
  Nur Typen/IDs/Zähler/Hashes, kein Inhalt; gleiche harte Journal-Limits wie
  heute. `total_cost_usd` wird ignoriert; Proxy-Settlement ist maßgeblich.
- **Evidenz** `AgentRunReceipt v2` (CAS + Ledger-Block via Gateway):
  Request-Digest, Images B/W/R, `session_id`, Modell(e), Exit-Code,
  `result`-Subtyp, Journal-SHA-256, Output-CAS-Refs, Proxy-Settlement-Refs,
  Zeitpunkte. Jeder Lauf ist nachspielbar und forkbar.
- **Output-Policy:** nur deklarierte Pfade (Factory: `factory-candidate.json`,
  `candidate.zip`, `test-evidence.json`), Größenlimits, keine Änderungen
  außerhalb des Workspace.

| Fehlerklasse | Beispiele | Verhalten |
|---|---|---|
| Infrastruktur | Spawn/Upload fehlgeschlagen, Operation-Timeout, Proxy unerreichbar | retriable (bestehende Retry-Autorität) |
| Agent | `result`-Fehler, fehlende Outputs, verbotene Änderungen | nicht retriable → IMPROVEMENT_REQUESTED |
| Budget | Proxy meldet erschöpft | terminal, Settlement wird geschrieben |
| Policy | unerlaubter Befehl/Env/Pfad | nicht retriable, vor dem Start |

## 6. Verträge und Benennung

- Neu, neutral: Runtime-Operationen `agent.run/resume/status/cancel/heartbeat`;
  Delivery-Events `agent_session_*`; Factory-Schritte `BRIEF_AGENT`,
  `SEAL_AGENT_BUILD`; Skill `captain-factory-seal-agent-build` (neu released);
  `captain.agent-build-receipt.v2`, `captain.agent-session-receipt.v2` mit
  `agent_kind="claude-sandbox"`, `model`, `sandbox_image_id`, `session_id`.
- Alt: codex-v1-Modelle wandern nach `agenten/legacy/codex_v1.py`, nur lesend,
  damit vorhandene Blöcke/Evidenz projiziert und verifiziert werden können.
  Neue `codex.*`-Commands werden vom Gateway abgelehnt.
- Import-Boundary-Test: `agenten.legacy.codex_v1` nur aus Gateway-Lesepfaden;
  nach M6 kein `codex`-Import außerhalb `legacy`.

## 7. Migration

| # | Schritt | Inhalt |
|---|---|---|
| S | Spike (Wegwerf) | Claude Code → Relay → Nemotron mit Tool-Roundtrip + Datei-Write, lokal und in ConTree. Klärt: Nemotron-Tool-Calling (Super vs. Ultra), Public-Image-Pull, Datei-Download/Output-Größe, Cancel, Egress-Begrenzung. |
| M0 | Provider-Switch | `CAPTAIN_LLM_BASE_URL`, `NEBIUS_API_KEY`, Nemotron-`model_info`; Evaluation-CLI, Benchmark, `live_demo_entrypoint` von hartem `openai` lösen; `LlmHouseholderExecutor` per Opt-in verdrahten (Demo bleibt deterministisch). |
| M1 | Kern | v2-Verträge, `AgentExecutionPolicy`, `ClaudeSandboxRunner`, `FakeContree`, Relay — noch ohne Aufrufer. |
| M2 | Gateway v2 | `agent.*`, `agent_session_*`, v2-Receipts annehmen; v1 → `legacy`, nur lesend. |
| M3 | Runtime | `service._dispatch` `agent.*` → Kern; `runtime_codex.py` + Codex-Adapter löschen; Adapter-Manifest neu erzeugen. |
| M4 | Delivery | `delivery/codex_runs.py` → Kern. |
| M5 | Factory | `BRIEF_AGENT`/`SEAL_AGENT_BUILD`; Hermes-Skills anpassen und neu releasen; `codex_build_*` löschen. |
| M6 | Aufräumen | `agenten/execution/codex_*`, `scripts/codex-session.ps1`, `provider_proxy/codex_config.py`, OpenAI-Responses-Transport + Tests löschen; `.env.example` `CAPTAIN_CODEX_*` → `CAPTAIN_AGENT_*` + `NEBIUS_*`. |
| M7 | Doku | `ARCHITECTURE.md` (Ausführung, ehrliche Ledger-Aussagen), `WORKSTREAMS.md`, `AGENTS.md`, `README` (Nebius/Nemotron). |
| D | Hosting (eigene Spec) | Nebius-VM mit Compose: MariaDB, Gateway, Proxy, Minibook; Reverse-Proxy/TLS; Bind-Adresse konfigurierbar. |

## 8. Tests und Gates

- Offline: Golden-Fixtures für Relay (Request, Stream, fragmentierte
  Tool-Args, lange Tool-Namen, Reasoning), Budget-Ablehnung/Settlement gegen
  Fake-Gateway; `FakeContree` mit aufgezeichneten `stream-json`-Fixtures;
  Resume, Output-Policy, Fehlerklassen, Receipt-Digests.
- Live (`-m live`, nur mit ausdrücklicher Freigabe, kostenpflichtig):
  Token-Factory-Roundtrip; ConTree + Proxy + Nemotron End-to-End.
- Pro Schritt: `pytest -m "not live"`, Fitness-/Boundary-Tests, `compileall`,
  `scripts/verify_submission.py`. `artifacts/demo-run.json` nur bei bewusstem
  Evidenz-Update neu schreiben.

## 9. Offene Punkte (durch Spike zu klären)

- Unterstützt Nemotron auf Token Factory Tool-Calling zuverlässig unter
  Claude-Codes Tool-Prompts? (Quellen widersprüchlich.) Welche Model-ID
  (`nvidia/Nemotron-3-Super-120b-a12b` vs. Kleinschreibung)?
- Kann ConTree öffentliche Registry-Images ziehen?
- Datei-Download bzw. Output > 10 MiB stdout?
- Cancel-API für laufende Operationen?
- Egress der Sandbox auf den Proxy begrenzbar?

## 10. Zeitplan

| Woche | Inhalt |
|---|---|
| 1 | Spike, M0, M1 |
| 2 | M2–M4 |
| 3 | M5, M6, Hosting (D) |
| 4 | Video, Devpost-Texte, Puffer |

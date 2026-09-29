# Goal-gesteuerter Agent-Builder — Design

Datum: 2026-09-30 · Branch: `feat/nebius-claude-sandbox`
Ergänzt: `docs/superpowers/specs/2026-09-29-nebius-codex-replacement-design.md`
(Relay, ClaudeSandboxRunner, v2-Verträge, Codex-Ablösung bleiben gültig).

## 1. Ziel

Ein einzelner Prompt erzeugt ein Goal. Aus dem Goal entwirft Hermes ein
AutoGen-Agententeam, das auf NVIDIA Nemotron (Nebius Token Factory) läuft,
Claude Code CLI als Produktionswerkzeug in Token-Factory-Sandboxes nutzt und so
lange iteriert, bis das Release-Gate des Captain grün ist. Fehlende Tools werden
als n8n-Workflows über das n8n-MCP erzeugt oder als Code-Bedarf gemeldet;
benötigte Zugangsdaten werden verfolgt und gemeldet. Alle Meldungen landen in
der Chain (Gateway-Ledger, Minibook-Feed).

Nicht-Ziele: neuer Goal-Mechanismus im Captain (Hermes `/goal` wird genutzt);
Personal-AI-Front-Door (NemoClaw) — optional, später; Änderungen am
`hermes-agent`-Submodul.

## 2. Ist-Zustand (Recherche 2026-09-30)

- Hermes `/goal`: `hermes-agent/hermes_cli/goals.py` (`GoalContract`,
  `GoalState`, `GoalManager`, `draft_contract()`, Judge-Schleife).
- Captain-Einstieg nur über strukturiertes `TO_BE_BUILT.md`
  (`input_document.py` → `input_compiler.py` → `job_builder.py` →
  `AgentFactoryJobV3`); kein Freitext.
- Team-Manifest `FactoryAutoGenTeamManifestV1`
  (`candidate_evaluation.py:101`): `conversation_pattern` ∈ swarm,
  selector_group_chat, round_robin_group_chat, single_agent; GraphFlow fehlt.
  `team_execution.py` instanziiert RoundRobin, Selector, Swarm.
- Tool-Auflösungsreihenfolge im Skill `autogen-agent-factory/SKILL.md:12`:
  released typed tool → native n8n node → approved n8n MCP operation → typed
  HTTP workflow → self-built local adapter → `TODO_TOOL.v1`.
- n8n: offizielles Instanz-MCP (`/mcp-server/http`), Captain-MCP-Broker mit
  Lease (`n8n_mcp_broker.py`), `TypedN8nTool`/`CaptainAuthorizedN8nTool`,
  Workflow-Erzeugung nur über Hermes-Profil `n8n-builder`
  (`create_workflow_from_code`).
- Tool-Lücken: `ToolGapMarker` (`TODO_TOOL.v1`), Release-Gate blockiert
  required+unresolved; Delivery-Event `hermes_tool_gap_recorded` existiert als
  Vertrag, wird aber nirgends emittiert.
- Zugangsdaten: n8n-seitig vollständig (`IntegrationCredentialRequirementV1`,
  Status missing…ready, Portal-Oberfläche); keine Modelle für Nicht-n8n-APIs,
  keine Push-Meldung bei `missing`.

## 3. Ablauf

```
main.py goal "<prompt>"   (alternativ: Hermes /goal mit Captain-Skill)
 1 Hermes /goal: draft_contract(prompt) → GoalContract (Ziel, Abnahmekriterien, Grenzen)
 2 Goal → TO_BE_BUILT.md (Skill) → vorhandener Parser → AgentFactoryJobV3
   Ledger: agent_factory_job mit goal_ref (Hash des GoalContract)
 3 Hermes Skill autogen-agent-factory → Team-Manifest, pattern = graph_flow
 4 Tool-Auflösung je Tool (Abschnitt 6) → Chain-Meldungen
 5 Captain-Freigabe: Lease, Budget, required Credentials = ready
 6 GraphFlow-Team läuft (Abschnitt 4), Builder nutzt claude_code_build (Abschnitt 5)
 7 Captain-Bewertung (Holdout, Benchmark)
    ├ grün → capability_promoted → Hermes-Goal-Judge: erfüllt
    └ rot  → IMPROVEMENT_REQUESTED → nächste Goal-Runde (max. 5 Versuche)
```

Der Hermes-Goal-Judge entscheidet nicht frei: Das Goal gilt genau dann als
erfüllt, wenn der Captain die Fähigkeit befördert hat. Der Judge liest dafür
den Gateway-Status (read-only).

## 4. Team-Schema: GraphFlow

Neuer Manifest-Wert `conversation_pattern = "graph_flow"` mit explizitem
Graph (Knoten = Agentennamen, Kanten mit optionaler Bedingung und
Aktivierungsgruppe). Standardvorlage:

```
Architect → Builder → Tester → Reviewer ─(APPROVE)→ Ende
               ▲                  │
               └─── (ITERATE) ────┘
```

- Umsetzung mit `DiGraphBuilder`/`GraphFlow` (autogen-agentchat 0.7);
  getrennte `activation_group` für Erstaktivierung und Feedback-Kante.
- Kantenbedingungen lesen Text-Token `APPROVE`/`ITERATE` des Reviewers,
  solange Nemotron-`json_schema` nicht per Spike bestätigt ist; danach optional
  `output_content_type=Verdict`.
- Termination: `MaxMessageTermination | TokenUsageTermination |
  TimeoutTermination | ExternalTermination` (letztere vom Budget-Wächter).
- Pro Agent `BufferedChatCompletionContext`; `parallel_tool_calls=False`;
  `max_tool_iterations` je Rolle aus dem Manifest.
- `save_state()` nach jeder Graph-Runde in den Factory-Workspace; Resume über
  `load_state()`.
- Manifest-Validierung: Graph muss einen Einstieg, einen Ausgang ohne
  Bedingung-Sackgasse und nur Knoten aus `agents` haben; Kanten-Bedingungen
  sind auf eine Allowlist (`APPROVE`, `ITERATE`, Tool-Name) beschränkt — keine
  frei ausführbaren Lambdas aus dem Manifest.
- Selector/Swarm/RoundRobin bleiben unverändert verfügbar.

## 5. Claude Code als Goal-gesteuertes Tool

Neues Captain-autorisiertes Tool `claude_code_build(goal: AgentGoalV1)`,
aufgebaut wie `CaptainAuthorizedN8nTool` (Grant-Recheck pro Aufruf,
Aufruf-Limit, Budget). Intern: `ClaudeSandboxRunner` aus der
Codex-Ablösungs-Spec.

```
AgentGoalV1 = {
  goal_id, parent_goal_ref,          # Kette Team-Goal → Builder-Goal
  objective,                          # Was gebaut/geändert werden soll
  acceptance_commands[],              # z. B. ["pytest -q tests/test_x.py"]; Judge
  constraints[],                      # Stil, Verbote, Schnittstellen
  allowed_paths[],                    # Output-Policy
  max_rounds, max_turns_per_round, budget_usd, timeout_seconds
}
```

Goal-Schleife im Runner:
1. Runde 1: `claude -p <gerenderter Goal-Prompt> --max-turns N` auf dem
   Workspace-Image → Image R1, `session_id`.
2. Judge: `acceptance_commands` als eigene ConTree-Operation auf R1
   (unabhängig von Claudes Aussage).
3. Nicht erfüllt: Runde k mit `claude --resume <session_id> -p <gekürzte
   Fehlerausgabe + offene Kriterien>` → Rk; zurück zu 2.
4. Ende bei erfüllt, `max_rounds`, Budget oder Timeout →
   `AgentGoalResultV1 { status, rounds[], final_image, acceptance_report,
   settlement_refs }`.

Der Goal-Prompt wird aus einer versionierten Vorlage gerendert
(`agenten/agent_factory/prompts/claude_goal_v1.md`): Ziel, Abnahmebefehle,
Grenzen, erlaubte Pfade, Hinweis „arbeite, bis die Abnahmebefehle grün sind;
ändere keine Tests, außer das Goal verlangt es“. Jede Runde = eigener
Checkpoint und eigener Ledger-Eintrag (`agent_goal_round`).

Zweites Tool `sandbox_run_tests(image, commands)` für den Tester: reine
ConTree-Operation, kein LLM.

Die Codex-Ablösungs-Spec (Runtime `agent.run`, Delivery, Factory-Build)
verwendet denselben Runner; dort wird ebenfalls `AgentGoalV1` statt eines
losen Prompts übergeben.

## 6. Tool-Auflösung und Chain-Meldungen

Für jedes Tool im Manifest (Reihenfolge laut `SKILL.md:12`):

| Ergebnis | Aktion | Chain-Meldung (Delivery-Event) |
|---|---|---|
| In n8n abbildbar | Hermes `n8n-builder` erzeugt Workflow im **Captain-eigenen** n8n über das offizielle n8n-MCP (`create_workflow_from_code`), Captain registriert `TypedN8nTool` | `tool_workflow_created` {tool, workflow_id, workflow_digest} |
| Nicht abbildbar | `TODO_TOOL.v1` mit `needs = "optional_code"` (oder `required_code`) | `hermes_tool_gap_recorded` {gap_id, tool, needs, severity, reason} — Text: „Tool X braucht optionalen Code für Y“; Builder erhält ein `AgentGoalV1` für einen lokalen Adapter → danach `tool_gap_resolved` |
| Braucht Zugangsdaten | Manifest-Tool bekommt `required_credentials[]` (Aliasnamen). n8n: vorhandener `IntegrationSetupPlanner`. Andere APIs: neu `ExternalCredentialRequirementV1` {alias, provider, env_var_name, scope} — nie Werte | `integration_credential_required` {alias, provider, tool, status}; bei `ready` → `integration_credential_ready` |

- Neue Delivery-Event-Typen: `tool_workflow_created`, `tool_gap_resolved`,
  `integration_credential_required`, `integration_credential_ready`;
  `hermes_tool_gap_recorded` wird erstmals emittiert und um `needs` erweitert.
- Alle Events werden in den Minibook-Feed projiziert.
- Release-Gate: blockiert bei required+unresolved Gap und bei required
  Credential ≠ `ready` (bestehende Logik für n8n, erweitert für externe APIs).
- Workflow-Erzeugung ist ein Live-Effekt im Captain-n8n (Port 5679, nie das
  VibeMind-n8n) und läuft nur mit gültigem Lease.

## 7. Sicherheit

- Keine Secret-Werte in Goal, Manifest, Events, Prompts oder Ledger — nur
  Aliasnamen und Env-Var-Namen.
- `claude_code_build` bekommt in der Sandbox nur das kurzlebige Relay-Token.
- Manifest-Kantenbedingungen sind deklarativ (Allowlist), kein Code.
- n8n-Workflows werden per Digest registriert; Änderungen erzeugen neue
  Version + Event.

## 8. Tests

- Offline: GraphFlow-Team mit Replay-Clients (Architect→…→Reviewer, ITERATE
  dann APPROVE); Manifest-Validierung (ungültige Graphen, nicht erlaubte
  Bedingungen); Goal-Schleife gegen `FakeContree` (Runde 1 rot, Runde 2 grün;
  max_rounds; Budget); Tool-Auflösung mit Fake-n8n-MCP (abbildbar/nicht
  abbildbar/Credential fehlt) und Event-Emission; Release-Gate-Blockaden.
- Live (`-m live`, nur mit Freigabe): Goal-Prompt → Team → ein
  `claude_code_build` in ConTree → Captain-Bewertung; ein n8n-Workflow über
  das Captain-n8n-MCP.

## 9. Reihenfolge (bis 2026-10-30)

| Schritt | Inhalt |
|---|---|
| Spike | wie Codex-Spec S1–S4, plus: AutoGen-Tool ruft `claude_code_build` (Fake-Runner) in einem GraphFlow auf |
| M1 | ClaudeSandboxRunner + Relay + `AgentGoalV1`-Goal-Schleife + `claude_code_build`/`sandbox_run_tests` |
| G1 | `main.py goal`, Hermes-/goal-Anbindung, Goal→TO_BE_BUILT.md, `graph_flow` in Manifest + `team_execution` |
| G2 | Tool-Auflösung, n8n-Workflow-Erzeugung, vier neue Events + Emission von `hermes_tool_gap_recorded`, `ExternalCredentialRequirementV1`, Release-Gate |
| M2–M6 | Codex-Ablösung in Runtime/Delivery/Factory (nutzt denselben Runner) |
| D | Hosting, Video, Devpost |

## 10. Offene Punkte

- Unterstützt Nemotron zuverlässig Tool-Calling unter AutoGen und
  `multiple_system_messages`? (Spike S1)
- Hermes-`/goal`-Judge: lässt er sich per Skill/Config auf einen externen
  Status (Gateway) statt eines Judge-Modells festlegen, ohne das Submodul zu
  ändern? Falls nein: Captain liefert dem Judge ein eindeutiges Statusdokument.
- Laufzeit von `claude_code_build` gegenüber MCP-/Tool-Timeouts (AutoGen
  `sse_read_timeout`); ggf. asynchron mit Polling.

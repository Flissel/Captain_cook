# Captain Zielbild und verbindliche TODOs

**Status:** aktiv  
**Owner:** Captain Core  
**Source of truth:** Captain-validierte, versionierte Zustände und Evidence im
Gateway/Ledger. AutoGen, Hermes, Codex, Minibook und n8n sind begrenzte
Ausführungs- oder Projektionspartner, nie Lifecycle-Autorität.

## Ziel

Captain nimmt eine Projektanforderung entgegen und führt sie nachvollziehbar
von der Analyse bis zur freigabefähigen Ausführung:

1. Er normalisiert und versioniert die Eingabe samt Digest und Herkunft.
2. Er lässt ein Reasoning-Modell ausschließlich über eng typisierte,
   Captain-kontrollierte Tool Calls eine Komponenten-Inventur, vollständige
   Subtask-Pläne und QA-Entscheidungen vorbereiten.
3. Er validiert die resultierende, versionierte Task-DAG deterministisch gegen
   Verfassung, Fähigkeiten, Abhängigkeiten, Workspace-Locks und Holdout-Regeln.
4. Er gibt nur freigegebene, dependency-bereite Subtasks an den passenden
   Worker frei. Codex erhält dabei ausschließlich einen Captain-fenced Run;
   n8n-MCP nur mit einer kurzlebigen `integration_intent=n8n`-Lease.
5. Er schreibt jede Transition, Tool-Reservierung, Entscheidung, Retry,
   Abbruch und Evidenz append-only in den Captain-Gateway/Ledger-Record,
   sodass ein Neustart weder Doppelarbeit noch Budget- oder State-Bypasses
   erzeugt.
6. Er liefert einen lesbaren, redigierten Report: akzeptierte und offene
   Komponenten, geplante Tests, Ausführungsstatus und belegte Grenzen.

## Definition of Done

Captain gilt erst als fertig, wenn eine reale, begrenzte LLM-Auswertung der
kanonischen AgentFarm-Eingabe mindestens ein Captain-validiertes Inventory,
einen QA-geprüften Komponentenplan und einen `accepted`-Report erzeugt. Der
anschließende Captain-Planungs-/Freigabepfad muss das Ergebnis als versionierte
DAG an den Gateway-fenced Worker-Run übergeben können. Alles muss zusätzlich
deterministisch wiederholbar und restart-sicher geprüft sein.

## Grenzen

- Captain besitzt Lifecycle, Validierung, Capability-Leases, DAG und Ledger.
- Hermes plant oder liefert Code-Artefakte, erhält aber keine Freigabehoheit.
- Minibook ist Projektion/Kollaboration, nicht Source of Truth.
- VibeMind besitzt seine n8n-Instanz und deren Volumes. Captain verwendet nur
  die ausdrücklich vorgesehene Integration-Schnittstelle.
- Kein Prompt oder Modelltext darf Captain-Validierung, Budgets, Pfadschutz,
  Redaction oder den Gateway umgehen.

## TODOs und Evidenz

- [x] Captain-eigene Eingabe-, Inventar-, Subtask-, QA- und Report-Verträge
  implementiert; Artefakte sind append-only, redigiert und digest-gebunden.
- [x] Bounded Society-of-Mind-Toolkette implementiert: Quelle lesen → Inventar
  stage → Komponentenplan stage → QA stage; Tool-Schemas sind typisiert und
  Captain-finalisiert den Lauf.
- [x] Persistierte Provider-Call-Reservierungen, Resume-/Restart-Grenzen,
  Fehlerterminalisierung und sichere CLI-Evidenz implementiert.
- [x] Vollständiger non-live Branch-Gate nachgewiesen: `894 passed, 78 skipped,
  16 deselected`, Coverage 81,23 % (`python -m pytest -q`, 2026-07-19).
- [x] Reale LLM-Evaluierung mit kanonischer AgentFarm-Eingabe bis zu einem
  `accepted`-Manifest geführt: `gpt-5.6-sol`, vier Provider-Aufrufe, 4.825
  Tokens, akzeptierter Komponentenplan und QA-Review. Der einzelne Live-Test
  bestand; sein isolierter Prozess meldete ausschließlich die globale
  Coverage-Schwelle als Exit-Status.
- [x] Akzeptierten Evaluierungsplan in eine versionierte,
  capability-validierte WorkBatch-DAG überführt und checkpointed Release
  getestet (`771e042`; `tests/planning/test_evaluation_bridge.py`). Die
  Brücke akzeptiert nur persistierte QA-Entscheidungen, bindet Quellen/DAG
  deterministisch und veröffentlicht einen identischen Run nie doppelt.
- [x] Einen einzelnen dependency-bereiten Subtask durch den Captain-fenced
  Hermes/Codex-App-Server-Run bis zur terminalen Gateway-Evidenz beweisen und
  das daraus erzeugte Artefakt auf dem isolierten Captain-n8n-Builder deployen
  und ausführen (`tests/live/test_gate_a_codex_n8n.py`, 1 passed in 30,32 s;
  2026-07-19). Der Test nutzt eine disposable MariaDB, einen temporären
  Workspace und den Builder auf Port 5679; VibeMind und seine Volumes bleiben
  unverändert.
- [x] Ablauf einer expliziten, kurzlebigen `integration_intent=n8n`-MCP-Lease
  durch einen Captain-fenced Codex-Run live bewiesen
  (`tests/live/test_agent_runtime_n8n_live.py`, 1 passed in 23,22 s;
  2026-07-19). Codex erstellt den begrenzten Workflow, Hermes ruft über den
  Captain-gebundenen n8n-MCP-Server dessen Validierung, Erstellung, Test,
  Veröffentlichung, Ausführung und Archivierung auf. Nach Lease-Ablauf
  verweigert der Test sowohl die MCP-Entdeckung als auch eine neue
  Hermes-Referenz; er verwendet ausschließlich den isolierten Builder auf Port
  5679, nicht die externe VibeMind-n8n-MCP-Verbindung.
- [x] Einen expliziten Captain-seitigen Widerruf einer noch gültigen
  `integration_intent=n8n`-Lease implementiert (`2d03993`): ein append-only
  Gateway-Revocation-Event bindet Grant und Command, blockiert danach neue
  Runtime-Effekte und Ergebnisse und verweigert jede weitere Hermes-/n8n-MCP-
  Referenz. Der MariaDB-Gateway-Gate bestand mit `6 passed`; der reale
  Codex-/n8n-Live-Gate mit zusätzlicher Revocation-Prüfung bestand mit
  `1 passed in 29,98 s` (2026-07-19).
- [x] Einen laufenden Codex-Prozess bei einem persistierten Lease-Widerruf
  über den vorhandenen, sessiongebundenen Process-Tree-Canceller abbrechen;
  ein grant- und command-gebundener Monitor gewinnt gegen den laufenden Run
  und bewahrt dessen terminale Gateway-Abbruch-Evidenz (`48f261b`; `209
  passed, 6 skipped`, inklusive realem PowerShell-Process-Tree-Abbruch).
  Der echte LLM-/MCP-Race-Gate bleibt separat offen.
- [x] Einen echten nativen Codex-Provider-Prozess mit pro-Lease n8n-MCP-
  Konfiguration während eines Captain-Lease-Widerrufs live abbrechen
  (`9cfb9fe`; `tests/live/test_n8n_mcp_broker_live.py`). Während der Prozess
  nachweisbar aktiv ist, gelingt eine n8n-MCP-Entdeckung unter demselben
  Grant/Token; der persistierte Captain-Widerruf terminiert den Run
  sessiongebunden als `captain_revoked`, der Broker verweigert das Token danach
  mit HTTP 403 und es bleibt genau eine terminale Gateway-Evidenz. Der
  isolierte Live-Gate bestand auf integriertem `main` mit `3 passed in
  22,45 s` (2026-07-19); der zugehörige Gateway-Runner mit `1061 passed,
  1 skipped, 13 deselected` in 76,79 s. Der Test rekonstruiert nicht den
  Modell-Toolcall aus Codex-Logs,
  sondern beweist Provider-Laufzeit und MCP-Wirkung unter derselben Lease.
- [x] Den statischen n8n-MCP-Instanzschlüssel durch einen Captain-eigenen,
  kurzlebigen Lease-Broker/Proxy ersetzen. Eine Rotation des jetzigen
  instanzweiten Schlüssels wäre kein sicherer Einzelwiderruf, weil sie
  parallele, rechtmäßige Runs ebenfalls unterbrechen würde. Token-, Proxy- und
  Referenzkern (`c469caa`, `1f8a902`, `cdcb48b`), isolierte Compose-Laufzeit
  und Gateway-Reader sind verbunden. Der echte Gate
  `tests/live/test_n8n_mcp_broker_live.py` erzeugt ein temporäres MariaDB- und
  Gateway-Paar, entdeckt n8n-MCP einmal über den Broker und erhält nach einem
  persistierten Gateway-Widerruf mit demselben Lease-Token HTTP 403
  (`6582e93`; `1 passed in 7,56 s`, 2026-07-19). Der Broker wird ausschließlich
  gestoppt; Builder-n8n, VibeMind und Volumes bleiben unverändert.
- [x] Aktive Codex-Sessions nach einem Neustart fail-closed einzäunen
  (`0661ca4`): der Gateway-Event-Append liefert jetzt auch seinen
  Replay-Status; bei einer schon aktiven Session startet der Supervisor keinen
  zweiten Provider-Run, sondern meldet `evidence_unresolved`. Ein
  Parallel-Restart-Test lässt genau einen Session-Owner zu.
- [x] Den E2E- und Recovery-Pfad nach Prozessneustart mit unveränderten
  Gatewaydaten geprüft: Nach einem echten Neustart des temporären
  FastAPI/MariaDB-Gateways terminalisiert Captain eine verlorene, persistierte
  Codex-Session einmalig unter ihrem vorhandenen Fence, zeichnet genau eine
  `claim_expired`-Requeue auf und erlaubt erst danach eine neue, gefencete
  Claim-Iteration samt neuer Session. Der echte Gate
  `tests/live/test_n8n_mcp_broker_live.py` bestand mit `4 passed in 27,15 s`
  und der Gateway-Runner mit `1066 passed, 1 skipped, 14 deselected` (2026-07-19).
  `GATEWAY_CLAIM_TTL_SECONDS` ist streng positiv konfigurierbar (Default
  5.400 s); die eine Sekunde gilt ausschließlich für den isolierten Test.
  Captain reattached keinen nicht beobachtbaren fremden Provider-Stream.
- [x] Einen produktiven Captain-Startaufrufer für abgelaufene Gateway-Leases
  bereitgestellt: `python main.py recover-gateway` verwendet ausschließlich
  das Captain-Token, gibt nur maschinenlesbare `recovered_batch_ids` und
  `deferred_batch_ids` aus und fährt bei fehlender Terminal-Evidenz fail-closed
  fort. `start.ps1` startet Gateway und MariaDB, prüft `/healthz` und führt
  diesen Pass vor den abhängigen Diensten aus. Die CLI-/Start-Verträge sind mit
  `15 passed` geprüft (2026-07-19); die Konfiguration erzeugt lokale
  Gateway-Tokens und eine loopback-DSN nur in der gitignorierten `.env`.
- [ ] Einen lokalen Codex-Worker-Recovery-Director an den Startpfad binden,
  der die zu einer aktiven Session gehörende, verifizierte Prozessidentität
  sicher findet, den Baum terminalisiert oder als verloren beweist und erst
  dann den bereits vorhandenen Captain-Requeue auslöst. Solange eine solche
  host-lokale Evidenz nicht vorhanden ist, bleibt der Batch bewusst
  `deferred`; das ist korrektes Fail-Closed-Verhalten, aber keine vollständige
  automatische Worker-Recovery.
- [x] Gesamt-Readiness prüfen: vollständiger non-live Gate, explizite
  Live-Gates, Architektur-/Importgrenzen, Demo-Evidenz und branch-sichere
  main-Integration. Aktuell: `986 passed, 79 skipped, 14 deselected`
  (`python -m pytest -q --no-cov -m "not live"`, 2026-07-19),
  Architektur-/Import-/Workstream-Gates mit `18 passed`, erfolgreicher
  Compile der Captain-Pakete und Demo-Evidenz mit vier terminalen
  Subproblemen sowie erfolgreiche Builder-, Gate-A-, n8n-MCP- und
  Broker-Revocation-Live-Gates.
  Der aktuelle isolierte Gateway-Runner lief mit `1064 passed, 1 skipped,
  14 deselected`; der eine Skip ist der absichtlich nicht in-process testbare
  No-AutoGen-Degradationspfad. Skips ersetzen weder eine separat grüne
  Gateway-Teilmenge noch die Live-Nachweise.

## Pflege-Regel

Jede erledigte Checkbox muss den Commit, den exakten Test-/Live-Nachweis und
offene Skips oder Limits in diesem Dokument oder im zugehörigen dated Plan
referenzieren. Ein fehlender oder roter Live-Gate bleibt offen und wird nie als
grün umformuliert.

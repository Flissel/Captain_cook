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
- [ ] Eine explizite, kurzlebige `integration_intent=n8n`-MCP-Lease inklusive
  Ablauf und Widerruf durch einen Captain-fenced Codex-Run live beweisen. Der
  grüne Gate-A-Workflow verwendet bewusst den isolierten Builder-HTTP-Target,
  nicht die externe VibeMind-n8n-MCP-Verbindung.
- [ ] Den E2E- und Recovery-Pfad nach Prozessneustart mit unveränderten
  Artifacts/Gatewaydaten prüfen: keine doppelte Provider-Reservierung,
  Freigabe oder Ledger-Transition.
- [ ] Gesamt-Readiness prüfen: vollständiger non-live Gate, explizite
  Live-Gates, Architektur-/Importgrenzen, Demo-Evidenz und branch-sichere
  main-Integration. Aktuell: `858 passed, 78 skipped, 7 deselected`
  (`python -m pytest -q --no-cov -m "not live"`, 2026-07-19) sowie
  `3 passed in 30,57 s` für Builder- und Gate-A-Live-Tests. Die ausgelassenen
  Tests benötigen eine separat konfigurierte MariaDB; sie sind kein Ersatz
  für den Gate-A-Nachweis.

## Pflege-Regel

Jede erledigte Checkbox muss den Commit, den exakten Test-/Live-Nachweis und
offene Skips oder Limits in diesem Dokument oder im zugehörigen dated Plan
referenzieren. Ein fehlender oder roter Live-Gate bleibt offen und wird nie als
grün umformuliert.

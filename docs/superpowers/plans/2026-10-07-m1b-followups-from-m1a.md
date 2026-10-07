# M1b-Folgearbeiten aus M1a (2026-10-07)

Quelle: Reviews und Sicherheitsprüfungen während der Umsetzung von
`docs/superpowers/plans/2026-10-01-m1a-goal-coding-core.md`. Diese Punkte sind
in M1a bewusst offen geblieben und müssen im M1b-Plan (echter ConTree-Adapter,
Nebius-Transport, `/v1/messages`-Route) erledigt werden.

## Judge-Integrität (Abnahme)
- [ ] Abnahme in einem **frischen Judge-Image** ausführen: Basis-Image + nur die Workspace-Dateien des Runden-Images; nie im von Claude veränderten Image (Restrisiko R6: Änderungen außerhalb `/work`, z. B. site-packages, sind heute unentdeckt).
- [ ] Danach Denylist geschützter Dateien (conftest.py, pytest.ini, pyproject.toml, …) auf Defense-in-depth zurückstufen und dokumentieren (R8).
- [ ] `.git` ist aus dem Manifest ausgenommen und wird nicht bereinigt — im frischen Judge-Image nicht mitkopieren oder explizit prüfen.
- [x] Symlink-Retargeting auf inhaltsgleiche Datei erkennen — in M1a erledigt: Symlinks werden per Linktext erfasst und nie verfolgt; neue/umgezielte Links, die absolut sind, aus `/work` heraus oder nach `.git` zeigen oder auf ein Verzeichnis zeigen, sind Policy-Verstöße (`unsafe_symlinks`).
- [ ] Änderungserkennung (Manifeste) ebenfalls mit vertrauenswürdigen Werkzeugen ausführen — frisches Judge-Image oder hostseitiger Export —, nicht über den `PATH` des vom Agenten veränderten Images (`sha256sum`/`find` könnten ersetzt sein).
- [ ] Reine Modusänderungen (`chmod +x`) erkennt das Manifest nicht.
- [ ] Annahme „Sandbox-Shell ist bash“ (`set -o pipefail`) im Basis-Image festschreiben und im Adapter prüfen.

## Budget und Token
- [ ] Run-Token beim Proxy **an `AgentGoalV1.budget_usd` binden** (harte Obergrenze pro Goal); der Runner prüft `budget_usd` heute nicht selbst (R12).
- [ ] Run-Token-Ausgabe `POST /v1/captain/run-tokens` über den Runtime-Finalize-Kanal; Proxy speichert nur Hash.
- [ ] Run-Token-Issuer am Proxy implementieren (Port `RunTokenIssuerPort.issue(goal)`/`revoke(goal_id)` existiert seit M1a): an `goal.budget_usd` gebunden, kurze Gültigkeit, Widerruf bei Goal-Ende. Redaktion des Tokens bleibt nur Defense in depth.

## Relay-Route
- [ ] `/v1/messages`-Route: ein `UpstreamFormatError` mitten im Stream muss als terminales Anthropic-`error`-Event bzw. Verbindungsabbruch enden, nie als sauberer SSE-Abschluss.
- [ ] `BridgeError` → `400 invalid_request_error`, `UpstreamFormatError` → `api_error`; Budget erschöpft → `400 invalid_request_error`.
- [ ] Live-Spike prüfen, ob die strenge Tool-Paarung (R17) bei Claude-Code-Unterbrechungen (Text ohne tool_result) echte Anfragen ablehnt.
- [ ] `finish_reason: content_filter` wird heute auf `end_turn` abgebildet — Fehler-/Refusal-Semantik entscheiden.

## Goal-Schleife
- [ ] Ausnahmen (ungültige Session-ID, Stream zu groß, Manifest-Protokollfehler) als `failed`-Ergebnis mit erhaltenen Runden statt als Exception.
- [ ] Goal-Timeout auch während Manifest/Abnahme durchsetzen, nicht nur am Rundenstart.
- [ ] Evidenz-Sink an Gateway/Ledger anbinden (`agent_goal_round`-Blöcke).
- [ ] `cancellation_token` in `claude_code_build`/`sandbox_run_tests` an den Sandbox-Adapter durchreichen.

## GraphFlow
- [ ] Stall-Erkennung für Fan-out-Graphen mit bedingtem Zweig härten (heute: „letzter Sprecher ist Blatt“).
- [ ] `business_benchmark_candidate_seeds`-Drift-Prüfung um `graph` erweitern.

## Live-Spike (Claude Code im Sandbox-Image)
- [ ] Mit `--dangerously-skip-permissions` beschränkt `--allowedTools` die Tools möglicherweise nicht — `--disallowedTools`/`--tools` prüfen.
- [ ] Claude Code verweigert `--dangerously-skip-permissions` evtl. als root ohne `IS_SANDBOX=1` — Konflikt mit der exakten Env-Allowlist klären.
- [ ] `HOME`/`PATH`-Behandlung des Sandbox-Adapters festlegen (Env ersetzen vs. mergen).
- [ ] Claude Code schreibt `/work/.claude/*` — erscheint heute als geänderter Pfad (Policy-Verstoß außerhalb `allowed_paths`).
- [ ] Bilder/Dokumente in `tool_result` lassen heute die ganze Anfrage scheitern — Text-Platzhalter erwägen.
- [ ] Gestreamtes `message_start`-`usage` ist 0 — Auswirkung auf Claude Codes Auto-Compaction prüfen.

## Nachträge aus der Nachprüfung der M1a-Schlussrunde
- [ ] Basis-Image-Vertrag: GNU `find` (`-printf`) und GNU coreutils (`sha256sum -z`) zusätzlich zu bash festschreiben; Adapter prüft beim Start.
- [ ] Richtlinie für venvs: neue Verzeichnis-Symlinks (z. B. `.venv/lib64`) gelten heute als Verstoß — venv außerhalb von `/work` anlegen oder gezielte Allowlist definieren.
- [ ] Wenn `revoke` fehlschlägt, vorher das Endergebnis aufzeichnen (heute ersetzt die Revoke-Exception das Ergebnis und überspringt `record(result)`).
- [ ] `GoalEvidenceSinkPort.record_round`/`record` bei der Ledger-Anbindung asynchron machen.
- [ ] Schon vorhandene, nach außen zeigende Verzeichnis-Symlinks und ein `.git`-Symlink sind für das Manifest unsichtbar — im frischen Judge-Image ausschließen.
- [ ] Prüfen, ob der Sandbox-Adapter stdout kappt — ein gekapptes Manifest an einer Datensatzgrenze würde Änderungen verschlucken; Größe/Abschlussmarker prüfen.

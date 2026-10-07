# M1b-Folgearbeiten aus M1a (2026-10-07)

Quelle: Reviews und Sicherheitsprüfungen während der Umsetzung von
`docs/superpowers/plans/2026-10-01-m1a-goal-coding-core.md`. Diese Punkte sind
in M1a bewusst offen geblieben und müssen im M1b-Plan (echter ConTree-Adapter,
Nebius-Transport, `/v1/messages`-Route) erledigt werden.

## Judge-Integrität (Abnahme)
- [ ] Abnahme in einem **frischen Judge-Image** ausführen: Basis-Image + nur die Workspace-Dateien des Runden-Images; nie im von Claude veränderten Image (Restrisiko R6: Änderungen außerhalb `/work`, z. B. site-packages, sind heute unentdeckt).
- [ ] Danach Denylist geschützter Dateien (conftest.py, pytest.ini, pyproject.toml, …) auf Defense-in-depth zurückstufen und dokumentieren (R8).
- [ ] `.git` ist aus dem Manifest ausgenommen und wird nicht bereinigt — im frischen Judge-Image nicht mitkopieren oder explizit prüfen.
- [ ] Symlink-Retargeting auf inhaltsgleiche Datei erkennen (Manifest hasht heute das Ziel, nicht `readlink`).
- [ ] Annahme „Sandbox-Shell ist bash“ (`set -o pipefail`) im Basis-Image festschreiben und im Adapter prüfen.

## Budget und Token
- [ ] Run-Token beim Proxy **an `AgentGoalV1.budget_usd` binden** (harte Obergrenze pro Goal); der Runner prüft `budget_usd` heute nicht selbst (R12).
- [ ] Run-Token-Ausgabe `POST /v1/captain/run-tokens` über den Runtime-Finalize-Kanal; Proxy speichert nur Hash.

## Relay-Route
- [ ] `/v1/messages`-Route: ein `UpstreamFormatError` mitten im Stream muss als terminales Anthropic-`error`-Event bzw. Verbindungsabbruch enden, nie als sauberer SSE-Abschluss.
- [ ] `BridgeError` → `400 invalid_request_error`, `UpstreamFormatError` → `api_error`; Budget erschöpft → `400 invalid_request_error`.
- [ ] Live-Spike prüfen, ob die strenge Tool-Paarung (R17) bei Claude-Code-Unterbrechungen (Text ohne tool_result) echte Anfragen ablehnt.

## Goal-Schleife
- [ ] Ausnahmen (ungültige Session-ID, Stream zu groß, Manifest-Protokollfehler) als `failed`-Ergebnis mit erhaltenen Runden statt als Exception.
- [ ] Goal-Timeout auch während Manifest/Abnahme durchsetzen, nicht nur am Rundenstart.
- [ ] Evidenz-Sink an Gateway/Ledger anbinden (`agent_goal_round`-Blöcke).
- [ ] `cancellation_token` in `claude_code_build`/`sandbox_run_tests` an den Sandbox-Adapter durchreichen.

## GraphFlow
- [ ] Stall-Erkennung für Fan-out-Graphen mit bedingtem Zweig härten (heute: „letzter Sprecher ist Blatt“).
- [ ] `business_benchmark_candidate_seeds`-Drift-Prüfung um `graph` erweitern.

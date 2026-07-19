# Ready-to-use release gate

## Evidence chain

- [x] Run three distinct real Codex -> Captain-n8n -> Gateway/MariaDB cases
  under one candidate project/run pair.
- [x] Require Gateway-side evidence for successful Codex completion, sealed
  artifact, n8n deployment, and live validation before accepting release.
- [x] Persist the Captain-only `release_decision` through the Gateway API.
- [x] Project a redacted release validation event to Minibook and append a
  `registry_mirror` acknowledgement only after the HTTP projection succeeds.
- [x] Re-run the intentional orphaned-session recovery gate against the
  isolated MariaDB service.

## Repeatable command

With `TEST_MARIADB_DSN`, `OPENAI_API_KEY`, `CAPTAIN_N8N_API_KEY`, and
`CAPTAIN_N8N_PORT` configured, run:

```powershell
pwsh -NoProfile -File scripts/run-gate-e.ps1
```

The command creates one fresh candidate, performs all three provider-backed
iterations, and only then writes the Gateway release decision and Minibook
acknowledgement. It never targets the production ledger database.

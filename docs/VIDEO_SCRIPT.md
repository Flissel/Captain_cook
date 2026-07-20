# Devpost video run-of-show

Keep the public YouTube video under three minutes.

1. **0:00–0:20 — Problem.** “Building agents is easy; proving what they did is harder. Captain Cook records an agent-work lifecycle rather than hiding it behind a chat transcript.”
2. **0:20–0:45 — Architecture.** Show the README diagram and explain decomposition, gatekeeping, routing, worker execution, and ledger recording.
3. **0:45–1:25 — Live run.** Run `python main.py demo --output artifacts/demo-run.json`; show the four role-tagged subproblems and open the JSON evidence artifact. Point out each report's explicit offline limitation.
4. **1:25–1:55 — Engineering evidence.** Run `python -m pytest -q` and `python scripts/verify_submission.py`; point out that the demo is offline and reproducible.
5. **1:55–2:30 — Codex and GPT-5.6.** State specifically which implementation, tests, and documentation Codex accelerated; show the primary Codex session ID in the submission materials; explain the GPT-5.6-backed production path separately from the deterministic demo.
6. **2:30–2:55 — Why it matters.** Show the delivery-fleet roadmap and clearly label Hermes, gateway, n8n, and Minibook work as next-stage integrations.

Before uploading, verify spoken audio says both “Codex” and “GPT-5.6”, the recorded terminal shows an actual successful run, and no credentials are visible.

## Live Demo A2 insert: evidence chain

Use this only when the dedicated live recording test passes against the current
provider-backed export. Keep the raw export and environment panes off screen.

1. **Preflight (not recorded).** Verify presence—not values—of isolated
   MariaDB, Codex/provider, Captain n8n, Minibook, and evidence input. Run Gate E
   after every database-resetting test.
2. **Runtime.** Show the compact report's correlation ID, runtime gate, and
   opaque Codex session reference.
3. **Gateway and n8n.** Keep that ID visible while showing accepted release and
   succeeded execution. Never open credentials, headers, or raw payloads.
4. **Minibook.** Show readback and say Minibook is a read-only projection, not
   release authority.
5. **Recovery.** Show controlled recovery and state that the expected failure
   was observed first.
6. **Stability.** Show the three distinct normal-run references in order.
7. **Close.** Run the live recording test. A skip or block is not a green take.

Checklist: one correlation ID; six passed gates; one recovery plus three normal
runs; no `.env`, token, absolute path, raw prompt, holdout, or provider payload.

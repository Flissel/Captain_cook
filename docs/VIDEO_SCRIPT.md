# Devpost video run-of-show

Keep the public YouTube video under three minutes.

1. **0:00–0:20 — Problem.** “Building agents is easy; proving what they did is harder. Captain Cook records an agent-work lifecycle rather than hiding it behind a chat transcript.”
2. **0:20–0:45 — Architecture.** Show the README diagram and explain decomposition, gatekeeping, routing, worker execution, and ledger recording.
3. **0:45–1:25 — Live run.** Run `python main.py demo --output artifacts/demo-run.json`; show the four role-tagged subproblems and open the JSON evidence artifact. Point out each report's explicit offline limitation.
4. **1:25–1:55 — Engineering evidence.** Run `python -m pytest -q` and `python scripts/verify_submission.py`; point out that the demo is offline and reproducible.
5. **1:55–2:30 — Codex and GPT-5.6.** State specifically which implementation, tests, and documentation Codex accelerated; show the primary Codex session ID in the submission materials; explain the GPT-5.6-backed production path separately from the deterministic demo.
6. **2:30–2:55 — Why it matters.** Show the delivery-fleet roadmap and clearly label Hermes, gateway, n8n, and Minibook work as next-stage integrations.

Before uploading, verify spoken audio says both “Codex” and “GPT-5.6”, the recorded terminal shows an actual successful run, and no credentials are visible.

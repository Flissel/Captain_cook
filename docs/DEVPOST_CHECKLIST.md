# OpenAI Build Week submission checklist

Repository evidence available now:

- [x] Offline runnable project: `python main.py demo --output artifacts/demo-run.json`.
- [x] Committed run evidence: `artifacts/demo-run.json`.
- [x] Automated regression suite: `python -m pytest -q`.
- [x] Judge-facing integrity check: `python scripts/verify_submission.py`.
- [x] Video run-of-show: `docs/VIDEO_SCRIPT.md`.
- [x] Third-party notice: `docs/THIRD_PARTY_NOTICES.md`.
- [x] MCP operating guide: `docs/MCP_SETUP.md`.
- [x] Modular branch and Householder-agent plan: `docs/WORKSTREAMS.md`.
- [x] Local configuration template: ignored `.env`, loaded by the legacy LLM path.

## Verified locally on 2026-07-15

- Baseline: `feat/devpost-demo-readiness`; current modular implementation:
  `feat/householder-runtime` (after
  `feat/householder-runtime-contract`).
- `python -m pytest -q` → 165 passed, 1 skipped.
- `python main.py demo --output artifacts/demo-run.json` → 4 role-tagged
  subproblems reached `done`.
- `python scripts/verify_submission.py` → `Submission evidence check passed.`
- `codex mcp get n8n-mcp` → local instance-level endpoint registered at `http://localhost:15678/mcp-server/http`; connectivity remains conditional on a running n8n instance and authentication.

Owner actions before submission:

- [ ] Select and add a root project license, then publish the repository or share it with the required judging accounts.
- [ ] Put the OpenAI API key into the ignored local `.env` file before running `python main.py legacy`; never add it to Git or the Devpost video.
- [ ] Start n8n and enable instance-level MCP access, then perform one non-destructive n8n MCP connectivity check.
- [ ] Record and publish a public YouTube video under three minutes using `docs/VIDEO_SCRIPT.md`.
- [ ] Submit the Developer Tools category, project description, repository URL, and video URL in Devpost.
- [ ] Add the `/feedback` session ID from the Codex thread that contains most core implementation work.
- [ ] Verify the submission before the official deadline: Tuesday, July 21, 2026 at 5:00 PM Pacific Time.

# Video run-of-show — Nebius x NVIDIA Global AI Hackathon

Hard limit: **under 3:00**. Judges are not required to watch past three
minutes. Upload as a public YouTube video. No third-party trademarks beyond
naming the technologies used, no copyrighted music, no credentials on screen.

Every segment must show the real system working. Segments marked `PENDING`
depend on work that is not finished yet; if it is not live by recording day,
cut the segment instead of faking it.

| Time | Segment | On screen | Voice-over (draft) |
|---|---|---|---|
| 0:00–0:15 | Problem | Title card, then a coding agent claiming "all tests pass" | "Coding agents say they're done. Captain Cook makes them prove it, and records the proof." |
| 0:15–0:40 | Architecture | `docs/architecture/00-overview.md` component map | "Nemotron on Nebius Token Factory plans and judges the work. A constitution gate decides what is in scope. Hermes roles and AutoGen teams build. A hash-chained ledger records every step, and only the Captain can release." |
| 0:40–1:20 | Nemotron live | Terminal: `main.py demo --householder-executor llm`; then the JSON showing `model: nvidia/Nemotron-3-Super-120b-a12b` and the subproblems reaching `done` | "This run calls Nemotron 3 Super through Token Factory. Every model answer is re-checked by code before it counts." `PENDING: live run` |
| 1:20–2:05 | Coding goal | A goal round: the agent edits code, acceptance runs, manifest diff shows changed files; a second clip where the agent edits `conftest.py` and gets `policy_violation` | "The agent works in a sandbox until acceptance passes. If it tries to edit its own tests, it fails." `PENDING: sandbox adapter + relay` |
| 2:05–2:35 | Evidence | Gateway ledger blocks with hashes, then the read-only Minibook projection | "Each step is a block in an append-only chain with leases and fencing. Minibook shows it, but it cannot change it." |
| 2:35–2:55 | Close | Hosted demo URL, repository URL, `pytest` summary line | "Open source, MIT licensed, about four thousand offline tests. Try it at …" `PENDING: hosted URL` |

## Fallback cut (if sandbox and relay are not live)

Keep 0:00–1:20 and 2:05–2:55, and replace the coding-goal segment with the
offline acceptance tests (`tests/coding_agent/`) running, captioned
"offline contract tests; live sandbox run in progress". Do not imply a live
sandbox run.

## Recording checklist

- [ ] `.env`, keys, tokens, absolute user paths and raw prompts never visible.
- [ ] The terminal shows an actual successful run, not a replay.
- [ ] Spoken audio names "NVIDIA Nemotron" and "Nebius Token Factory".
- [ ] Total length checked after export (under 3:00).
- [ ] Video is public on YouTube; link added to the Devpost form.

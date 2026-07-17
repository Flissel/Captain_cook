# Independent Work Packages Master Gap TODO

> This is the canonical planning ledger on `NewPlans/branch`. Implementation
> checkboxes are updated only from fresh branch evidence; planning completion
> does not mean implementation completion.

## Package status

| Package | Current proof | Open gap | Implementation plan |
| --- | --- | --- | --- |
| Captain Core | typed planning/release pipeline and gateway exist | no mandatory result→validation→promotion authority chain | `2026-07-17-captain-authority-chain.md` |
| Minibook | API and thin Captain HTTP projector exist | projection cursor, replay, redaction, and drift rebuild absent | `2026-07-17-minibook-projection-boundary.md` |
| Minibook creation pipeline | runnable swarm/forge code exists | no stable job/result API, durable safe checkpoints, or isolated package gate | `2026-07-17-minibook-creation-pipeline.md` |
| Hermes + Codex + n8n MCP | Hermes has Codex and generic MCP surfaces | no Captain worker-envelope adapter and no correlated real n8n MCP evidence | `2026-07-17-hermes-codex-n8n-worker.md` |

## Cross-package gaps

- [ ] Freeze JSON fixtures for every v1 envelope before consumer work begins.
- [ ] Assign one branch/worktree owner per package; no two agents edit the same
  package plan, manifest, or contract file concurrently.
- [ ] Keep the Hermes submodule commit explicit; never absorb submodule-local
  modifications into the parent commit accidentally.
- [ ] Prove that no package imports another package's internal modules.
- [ ] Prove that holdout bodies and credentials never enter Minibook, Hermes
  reports, n8n inputs, committed fixtures, or logs.
- [ ] Add one contract compatibility gate that validates producer and consumer
  fixtures without starting live infrastructure.
- [ ] Add one separately invoked live acceptance gate with zero required skips.
- [ ] Record external ownership: VibeMind n8n is validated/called but never
  started, stopped, adopted, migrated, reset, or volume-managed.

## Branch allocation

| Order | Suggested branch | Exclusive write scope |
| --- | --- | --- |
| 1 | `feat/captain-authority-chain` | `agenten/planning`, `agenten/validation`, gateway authority endpoints/tests |
| 2 | `feat/minibook-projection-boundary` | `agenten/delivery/minibook_*`, projection tests and Minibook API contract only |
| 2 | `feat/minibook-creation-contract` | `minibook/swarm` contract/runtime extraction and its tests |
| 2 | `feat/hermes-codex-n8n-worker` | Hermes submodule branch plus parent pin; no Captain internals |
| 3 | `integration/independent-work-packages` | fixtures, compatibility gate, final docs; no feature invention |

## Integration gates

- [ ] `python -m pytest -q tests/planning tests/validation tests/gateway`
- [ ] `python -m pytest -q minibook/tests`
- [ ] Hermes focused Codex/MCP worker tests pass at the pinned submodule commit.
- [ ] `python -m pytest -q tests/contracts/test_work_package_compatibility.py`
- [ ] live E2E records real Captain, Minibook, Hermes, Codex, and n8n correlation
  IDs and reports zero required skips.
- [ ] `python -m pytest -q` and `python scripts/verify_submission.py` pass.

## Gap maintenance protocol

After every merged package branch, the planning agent rebases or merges current
`main` into `NewPlans/branch`, reruns the named focused gate, and updates only
this table and the affected package plan. Contradicted evidence is retained in
git history; checkboxes are never checked from another agent's prose report
without command output or committed artifacts.

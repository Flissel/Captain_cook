# Autonomous Captain input, planning, execution, and review

> Date: 2026-07-16
> Worktree: `C:\Users\User\Desktop\Captain_cook-main-integration`
> Source input: `C:\Users\User\Desktop\Captain_cook\Autogen_AgentFarm\input.md`
> Source SHA-256: `e55e667474a3b6a3d1a1dc6f927fec9ea67a247ea30ea61141c5b994495623ac`

## Goal

Captain receives the existing Markdown file as immutable source intent and
autonomously produces a canonical, dependency-ordered plan. Planning,
execution, and review are separately composed processes. Execution cannot use
caller-forged approval, unvalidated reused capabilities, self-reported success,
or builder-owned review as authority.

## Implemented in the current candidate

- [x] Pure UTF-8 Markdown parser with exact-byte SHA-256, safe logical source
  reference, fence-aware heading tree, deep immutable section tuples, and a
  provenance-rich Captain context.
- [x] Verified the real 185,292-byte AgentFarm input parses deterministically
  into 250 headings, including 50 H2 sections, without LLM or side effects.
- [x] Split `CaptainPipeline.compile()` from compatibility publication so a
  complete batch+holdout set exists before any release write.
- [x] Enforce deterministic planning policy: allowed capability vocabulary,
  canonical tag order, and run-wide golden/holdout isolation.
- [x] Compile one immutable canonical plan with stable topological ordering,
  sealed batch contracts, one package disposition, a minimum five-worker pool,
  and exact `HANDOFF TO WORKER <N>` markers.
- [x] Publish source, plan tree, public batch contracts, and isolated holdouts
  as one atomic run bundle; identical concurrent publication is idempotent
  only when every expected file still matches byte-for-byte.
- [x] Compile allowlist-constrained mixed-target DAGs, including an `n8n`
  work package followed by a dependent `autogen` package, through both the
  composition API and repeatable CLI `--allowed-target` options.
- [x] Bind plan review to plan digest and deterministic review ID; execution
  resolves that ID through a trusted read port rather than accepting a verdict
  object from its caller.
- [x] Preflight reused capabilities through typed validated projections before
  the first executor call, including target, contract, rubric, assertion,
  runtime, runtime-version, interface-schema, artifact-version, and
  validation-reference compatibility. The expanded work-batch schema is v2.
- [x] Require independent validation projections before a successful build can
  satisfy downstream dependencies.
- [x] Carry `run_id`, `trace_id`, `codex_session_id`, and artifact versions
  through execution requests, results, validation binding, and the aggregate
  execution record.
- [x] Add artifact review contracts that expose content hashes and versions,
  not build-workspace paths, and reject builder self-review.
- [x] Add AST fitness checks for Planning -> Review -> Execution import
  direction and authority separation.

## Next implementation TODOs

- [ ] Add gateway event/contracts for immutable `plan_created`, `plan_review`,
  `validation_run`, `execution_permit`, and their projection IDs.
- [ ] Implement production `ReviewDecisionReader`, `CapabilityStatusReader`,
  and `ValidationStatusReader` adapters over the MariaDB Ledger Gateway.
- [ ] Add fencing tokens, projection versions, and revocation rechecks so
  review/capability evidence cannot be replayed after authority changes.
- [ ] Run plan and artifact reviewers in a separate read-only sandbox process;
  the current injected callback is a contract seam, not sandbox evidence.
- [ ] Add typed organization extraction on top of the generic Markdown tree:
  source spans, agents, teams, reports-to links, diagnostics, and no fabricated
  CSO/team/tool defaults.
- [ ] Add bounded/chunked input-corpus retrieval so the 193k-character Captain
  context is not repeated wholesale in every model stage.
- [ ] Implement gateway plan-level atomic publication; the JSON bundle remains
  offline/demo evidence only.
- [ ] Connect approved packages to the Hermes/Codex worker loop and persist
  correlated run/trace/batch/worker/session evidence through the gateway.
- [ ] Prove mixed n8n + AutoGen composition, isolated holdouts, three failure
  paths, and three consecutive clean E2E runs.
- [ ] Integrate the current candidate through the branch-lock/spec-review/
  quality-review process before updating release claims.

## Acceptance gates

```powershell
python -m pytest -q tests/planning tests/review tests/execution `
  tests/test_agent_factory_process_boundaries.py
python -m pytest -q tests/test_architecture_fitness.py `
  tests/test_import_boundaries.py tests/test_workstream_docs.py
python -m compileall -q agenten
```

The full repository gate, skipped-test review, live MariaDB evidence, and real
integration evidence remain mandatory before merge or release.

## Current verification snapshot

- Focused planning/review/execution/architecture gate: 69 passed.
- `python -m compileall -q agenten`: passed.
- Default full pytest gate: collection blocked by the known P07B-owned duplicate
  `test_contracts` basename; no product test ran past collection in that mode.
- Diagnostic `--import-mode=importlib` full gate: 328 passed, 24 skipped,
  1 deselected, 1 warning, 77.82% coverage.
- Skips are not live proof: MariaDB/gateway cases require `TEST_MARIADB_DSN`,
  the legacy `autogen` compatibility test lacks that package, and one
  no-AutoGen degradation path cannot run while `autogen_core` is installed.
- Submission verifier passed and the offline demo completed four subproblems to
  `done`; neither result proves live n8n, Hermes, Codex, Minibook, or MariaDB.

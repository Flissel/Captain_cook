# Task 6 report: Gateway benchmark persistence and promotion gate

## Status

Implemented on `codex/hermes-factory-evaluation` from base `0396187`.
Captain/MariaDB remains the sole authority. The Gateway persists only the
redacted `BusinessBenchmarkSummaryV1`; no private suite, run receipt, or case
receipt is added by this task.

## TDD evidence

- Initial focused RED command:
  `python -m pytest -q --no-cov tests/gateway/test_agent_factory.py tests/gateway/test_factory_repository.py tests/agent_factory/test_state_machine.py tests/agent_factory/test_release_gate.py tests/agent_factory/test_service.py`
- Confirmed feature RED after fixture corrections:
  `6 failed, 79 passed, 9 skipped`.
- Failures covered the missing repository persistence/read methods, absent
  state-machine summary binding, missing suite ownership validation, and the
  coordinator's unresolved summary path.

## Implementation

- Added `factory_business_benchmark_summaries` through
  `GatewayStore._ensure_schema` with unique summary ID, artifact SHA, and
  job/correlation/version/attempt/candidate-SHA identity.
- Stores the artifact digest and the full canonical content digest. Identical
  replay is idempotent; changed content at any immutable identity returns 409.
- Locks conflicting identities with `FOR UPDATE` and commits the summary plus
  deterministic `captain_business_benchmark_validated` delivery event in the
  same transaction/cursor.
- Added Captain-only POST, reader GET by summary ID and canonical artifact SHA,
  typed event payload/union/trace mapping, and Captain event classification.
- Rejects baseline/unrelated candidates, stale attempts, foreign suite refs,
  and mismatched job/correlation/version before persistence.
- Extended Gateway and in-memory repository ports for record/read by summary
  ID and exact artifact ref, including HTTP 409 translation.
- Coordinator, state machine, and release gate now resolve and validate the
  exact passed summary and preserve all pre-existing technical, assertion,
  tool-gap, budget, recovery, provider, catalog, execution, and demo gates.
- Legacy evaluation payloads without benchmark fields remain parseable but
  promotion fails closed.

## Verification

- Focused Task 6 gate:
  `python -m pytest -q --no-cov tests/gateway/test_agent_factory.py tests/gateway/test_factory_repository.py tests/agent_factory/test_state_machine.py tests/agent_factory/test_release_gate.py tests/agent_factory/test_service.py`
  -> `86 passed, 10 skipped in 0.79s`.
- Gateway deterministic scope:
  `python -m pytest -q --no-cov tests/gateway`
  -> `201 passed, 93 skipped in 1.02s`.
- Full Agent Factory gate:
  `python -m pytest -q --no-cov tests/agent_factory`
  -> `642 passed in 12.74s`.
- Compile gate:
  `python -m compileall -q agenten gateway`
  -> exit 0.
- Whitespace gate: `git diff --check` -> exit 0, with only Git's existing
  LF-to-CRLF checkout warnings.

## MariaDB node and remaining concern

Exact command:
`python -m pytest -q -rs tests/gateway/test_agent_factory.py::test_business_benchmark_summary_is_restart_safe_and_rejects_changed_replay`

- Test result: `1 skipped`; reason: `TEST_MARIADB_DSN is not configured`.
- Command exit: 1 because the isolated single skipped node also triggered the
  repository-wide 70% coverage threshold (`17.36%` observed).
- This is a configuration skip and coverage-policy exit, not MariaDB success
  evidence. The restart/atomic replay behavior remains unverified against a
  live isolated `captain_test` database in this worktree.
- No VibeMind n8n service or volume was touched. No Task 7 provider/live
  adapter work was added, and nothing was pushed.

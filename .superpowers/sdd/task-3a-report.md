# Task 3A report: Codex execution policy

## Delivered

- Added `CodexExecutionPolicy.authorize()` and the secret-safe
  `AuthorizedCodexRun` output model.
- Authorizes only `codex exec --json <prompt>` requests with a complete
  delivery context.
- Resolves and fences project/workspace paths; traversal and symlink escapes
  are rejected before the local Git inspection.
- Rejects secret-path command arguments and dirty Git projects before launch.
- Filters the child environment by allowlisted key and excludes all retained
  environment values from model serialization and representation.
- Added the eight agreed optional trace/project fields to `CodexRunRequest`:
  `project_id`, `run_id`, `trace_id`, `batch_id`, `worker_id`, `claim_id`,
  `fencing_token`, and `project_root`. The policy fails closed when any is
  missing. `session_id` remains the existing required Codex identity.

## RED-GREEN evidence

1. RED: `python -m pytest -q --no-cov tests/execution/test_codex_policy.py`
   failed during collection because `agenten.execution.codex_policy` did not
   exist.
2. GREEN: focused policy test run passed (initially 9 tests).
3. Additional RED: a bare `codex exec build` was accepted, so the new JSONL
   allowlist test failed as expected.
4. GREEN: `python -m pytest -q --no-cov tests/execution/test_codex_policy.py tests/execution/test_codex_supervisor.py`
   passed: **33 passed**.
5. Full reachable non-live gate:
   `python -m pytest -q --no-cov --ignore=tests/execution/test_codex_events.py`
   passed: **500 passed, 72 skipped, 9 deselected**.

## Self-review

- Security: no unapproved command reaches the runner-facing authorized model;
  all filesystem boundary comparisons use resolved paths; environment values
  are excluded from `repr` and `model_dump_json`.
- Correctness: tests use an actual temporary Git repository and symlink escape
  where the operating system supports symlinks. The dirty-worktree check is
  local-only and runs before authorization returns.
- Maintainability: no findings after `git diff --check`; the policy is not
  wired into `CodexSupervisor` in this subtask.

## Known external blocker

The unfiltered full suite currently stops only because the separately owned,
untracked `tests/execution/test_codex_events.py` imports the not-yet-created
`agenten.execution.codex_events` Task 3B module. It was left untouched and is
not included in this commit.

## Brief exception

The original brief said not to modify `codex_supervisor.py`. The task owner
approved the narrow exception needed for the policy contract: the eight
trace/project fields above are optional on the request for backward
compatibility, while `authorize()` makes them mandatory for a launch.

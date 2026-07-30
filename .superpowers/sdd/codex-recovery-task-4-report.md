# Task 4: Factory benchmark v19 local recovery preparation

## Scope and paid-run boundary

Implemented local v19 preparation only. No `-Action Run`, secure wrapper,
provider, Docker, database, n8n, Gateway, or Minibook command was executed.
The secure wrapper was not changed; its process-only `OPENAI_API_KEY` boundary
remains intact.

## RED

The historical focused script suite was run before the original implementation.
It failed for the intended gaps:

- v19 seed/suite constants and canonical Plan fields were absent;
- Plan emitted only the old minimal result;
- `FactoryCodexBuildInterrupted` escaped the operator CLI instead of producing
  an exit-2 redacted checkpoint.

Historical command used for the isolated RED/GREEN script suite:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
py -3.11 -m pytest -q -o addopts='' tests/scripts/test_run_business_benchmark_demo.py
```

## GREEN evidence

- `5 passed in 2.95s` for the focused script suite.
- `py -3.11 -m compileall -q agenten blockchain chats config gateway` exited 0.
- `py -3.11 scripts/verify_submission.py` exited 0 (`Submission evidence check passed.`).
- `git diff --check` exited 0.

The Plan fixture proves suite/seed v19, exactly Claims and Renewal dry-run job
scope, no `--apply`, and no provider, live-service, Gateway, or Minibook
mutation. A typed timeout fixture proves that the Python operator emits only
the redacted exit code, reason, checkpoint/receipt references, next resume
ordinal, and Captain resume binding fields; PowerShell validates and prints
that checkpoint unchanged, then exits 2 rather than converting it to a generic
failure.

## Dry-run execution status

The real local `-Action Plan` was not executed. Its isolated boundary fixture
uses poisoned `.env` and `.env.captain-n8n` files, an absent Hermes interpreter,
and an empty `PATH`; it still produces the canonical v19 Claims/Renewal plan.
Markers prove that no Run-only service, preflight, Factory, or provider path was
called. The Plan branch now resolves only Python 3.11 and invokes the
provisioner with explicit non-secret dry-run arguments. The tested,
reproducible operator command is:

```powershell
pwsh -NoProfile -File scripts/run-business-benchmark-demo.ps1 -Action Plan
```

It remains no-apply and does not invoke the service, provider, Factory runner,
Gateway, or Minibook paths.

## Corrected review evidence

The prescribed shared interpreter was available at
`C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe`.

- RED: the newly added Plan-isolation and interruption-validator boundary tests
  failed as intended: Plan stopped at the absent Hermes interpreter and the
  prior validator rejected the canonical `codex_timed_out` payload.
- GREEN: those two boundary tests passed (`2 passed, 5 deselected in 2.30s`).
- Focused offline suite passed:

  ```powershell
  C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/scripts/test_run_business_benchmark_demo.py tests/agent_factory/test_codex_build_execution.py tests/agent_factory/test_hermes_cli.py
  ```

  Result: `89 passed in 10.78s`.
- `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m compileall -q agenten` exited 0.
- `git diff --check` exited 0.

The validator tests reject host/local artifact URIs, URI/digest disagreement,
workspace traversal, malformed UUID and lease identifiers, zero cancellation
exit codes, and out-of-range resume ordinals. No provider, database, Docker,
n8n, Gateway, Minibook, or live service command was run.

## Ledger truth

The gap ledger now records local streaming/recovery as complete, v19 as
prepared only through no-apply Plan, and keeps provider execution, the 30-case
benchmark, Gateway promotion, and Minibook projection open. No v19 job IDs or
live evidence were created or claimed.

## Commit

`55d79d3 chore: prepare Factory benchmark v19 recovery`

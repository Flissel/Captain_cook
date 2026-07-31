# Implementer report

Status: DONE

Commit: `52aa17792c5a8839f0dc5eb5943223cce0a4dfa7`

Implemented `Build` so the two v19 Factory candidates can be provisioned, dispatched, sealed, and reported as `candidates_ready` before the live benchmark runner. Preserved unresolved post-Factory checkpoint/exit 2 behavior and unchanged `Run` behavior. Extended the secure wrapper with `Build|Run`, default `Run`, and action forwarding.

TDD evidence: the new behavior test first failed because `Build` was absent from the old `Plan;Run` ValidateSet; the secure-wrapper test also failed because no action was forwarded. After the minimal implementation, this command passed with 8 tests:

`C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/scripts/test_run_business_benchmark_demo.py tests/scripts/test_run_business_benchmark_demo_secure.py`

No live/provider/Docker action and no real `.env` read occurred during implementation.

## Review round 1 fix evidence

The action is normalized once after parameter binding, so accepted lowercase
`build` follows the same Factory-only stop as `Build` and cannot fall through
to the provider runner. `candidates_ready` now requires a resolvable preflight
to return exactly the provisioned Claims and Renewal `(job_id, candidate_id)`
pairs; an empty, duplicate, or mismatched scope fails closed before either
terminal path. The secure wrapper again keeps `PythonPath` as positional
argument zero and accepts `Action` as the later named `Build|Run` choice.

TDD evidence: new controlled-script regressions first exposed the lowercase
provider fallthrough and positional-wrapper failure. After the minimal fix,
the focused suite passed with 9 tests:

`C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe -m pytest -q --no-cov tests/scripts/test_run_business_benchmark_demo.py tests/scripts/test_run_business_benchmark_demo_secure.py`

No live/provider/Docker action and no real `.env` read occurred during this
review fix.

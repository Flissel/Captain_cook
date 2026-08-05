## RED

- Initial focused Task 5 command: `python -m pytest -q --no-cov tests\agent_factory\test_skill_workflow_contracts.py tests\agent_factory\test_team_evaluation.py tests\agent_factory\test_factory_feedback.py tests\agent_factory\test_skill_sequence.py tests\agent_factory\test_improvement.py` -> `51 failed, 81 passed`; failures demonstrated the missing benchmark binding, retry, feedback, and improvement contracts.
- Release-gate legacy bypass RED: `1 failed`; a v1 evaluation without benchmark evidence could still become ready.
- External ready-decision bypass RED: `1 failed`; the orchestration-ready path did not independently require benchmark evidence.
- Codex/Hermes consumer binding RED: `2 failed`; benchmark retry context was not carried to the brief or checked by Hermes.
- Missing receipt/tool evidence RED: `2 failed`; absent successful usage or required tool evidence was not classified as infrastructure failure.
- Canonical summary evidence RED: `1 failed`; a bound benchmark summary reference was not required in evaluation evidence.

## GREEN

- Interpreter: `C:\Users\User\Desktop\Captain_cook\.venv\Scripts\python.exe` (project Python 3.11 environment, invoked from this worktree).
- Focused Task 5: `python -m pytest -q --no-cov tests\agent_factory\test_skill_workflow_contracts.py tests\agent_factory\test_team_evaluation.py tests\agent_factory\test_factory_feedback.py tests\agent_factory\test_skill_sequence.py tests\agent_factory\test_improvement.py` -> `137 passed in 0.63s`.
- Agent Factory regression: `python -m pytest -q --no-cov tests\agent_factory` -> `619 passed in 13.09s`.
- Gateway repository/parser regression: `python -m pytest -q --no-cov tests\gateway\test_factory_repository.py` -> `27 passed in 0.49s`.
- Compile gate: `python -m compileall -q agenten` -> exit 0.
- Whitespace gate: `git diff --check` -> exit 0.
- Required four-file command without `--no-cov`: `python -m pytest -q tests\agent_factory\test_skill_workflow_contracts.py tests\agent_factory\test_team_evaluation.py tests\agent_factory\test_factory_feedback.py tests\agent_factory\test_improvement.py` executed all `126` tests successfully, then exited 1 solely because the repository-wide coverage gate observed `16.36%`, below `70%`.
- Skips: none in the deterministic no-coverage gates.

## Delivered invariants

- `TeamEvaluationService.evaluate` requires a keyword-only authoritative `BusinessBenchmarkSummaryV1`, validates exact job/correlation/subject-version/attempt/candidate/canonical-artifact bindings, and copies rather than recalculates benchmark policy results.
- Legacy Team Evaluation v1 payloads remain readable through optional/default benchmark fields, but cannot pass feedback, release-gate, or ready-decision promotion checks.
- Captain assertion IDs and benchmark metric IDs remain separate typed namespaces throughout evaluation, revision, authorization, Codex, and Hermes contracts.
- Behavioral benchmark failures authorize budget-aware retry; missing receipts/evidence remain infrastructure-only and cannot authorize revision.
- Benchmark reason codes survive feedback; prior-green metrics become explicit regression guards; failed assertions, failed benchmark metrics, or both can drive deterministic improvement.
- Improvement-surface mapping covers decision/rationale, tool contract, handoffs, completion/termination, and cost/latency model-client concerns without exposing private benchmark case content.
- Release and orchestration consumers independently require bound, passed benchmark evidence with no failed benchmark metrics.

## Concerns

- No live, provider, database, n8n, browser, or deployment boundary was exercised; Task 5 is intentionally deterministic.
- The required narrow pytest command is functionally green but cannot satisfy the global coverage threshold by itself; the full deterministic Agent Factory command is reported with `--no-cov` to separate functional evidence from partial-suite coverage pollution.
- Git reports only the checkout's LF-to-CRLF conversion warnings for modified files.

## Review-fix RED

- Reviewer-focused six-file RED command covering release, evaluation, feedback, Codex, Hermes, and skill-sequence boundaries -> `10 failed, 118 passed in 1.87s`.
- The failures reproduced the forged copied-field `demo_ready` bypass, missing-receipt overwrite of credential/preflight gates, stale and unauthoritative prior guards, absent typed Codex metric guards, missing Hermes comparison, and the assertion-specific benchmark uniqueness diagnostic.

## Review-fix GREEN

- Reviewer-focused eight-file gate: `python -m pytest -q --no-cov tests\agent_factory\test_skill_workflow_contracts.py tests\agent_factory\test_team_evaluation.py tests\agent_factory\test_factory_feedback.py tests\agent_factory\test_skill_sequence.py tests\agent_factory\test_improvement.py tests\agent_factory\test_release_gate.py tests\agent_factory\test_codex_brief.py tests\agent_factory\test_hermes_cli.py` -> `232 passed in 1.74s`.
- Agent Factory regression: `python -m pytest -q --no-cov tests\agent_factory` -> `640 passed in 14.63s`.
- Gateway parser/repository regression: `python -m pytest -q --no-cov tests\gateway\test_factory_repository.py` -> `27 passed in 0.54s`.
- Compile gate: `python -m compileall -q agenten gateway` -> exit 0.
- Whitespace gate: `git diff --check` -> exit 0.
- Skips and dependency warnings: none. Git reported only LF-to-CRLF conversion warnings for modified files.

## Review-fix invariants

- Pure workflow release and ready-decision boundaries now require a resolved authoritative summary argument, revalidate its canonical digest, and compare every job, correlation, version, attempt, candidate, policy, disposition, reason, failed-metric, and artifact binding.
- Existing service and Gateway call sites pass `benchmark_summary=None` and therefore fail closed until Task 6 resolves persisted summaries; no Task 6 database, repository, or API behavior was added.
- Existing credential, preflight, and other execution-specific failure classes retain precedence over benchmark missing-receipt classification and keep their exact feedback recommendation.
- Regression guards require the immediately preceding evaluation plus its independently resolved canonical summary; each attempt may bind a different candidate, but both evaluations must match their own authoritative summary.
- Codex briefs carry typed failed and regression benchmark metric IDs in both the sealed prompt digest and model, while Hermes compares both sets exactly to Captain's authorization.
- Prior-green benchmark duplicates now produce a metric-specific diagnostic.

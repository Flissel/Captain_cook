# Task 10 report — private benchmark authority Round 3

## Result and commits

- Status: `DONE`
- Required base: `c720e07cef4124b1bb1ec48736b2fc5951603d7a`
- Implementation commit: `50bfb3eabaf0157231f5ddf6f5027b3216c39545`
  (`fix(factory): ground private benchmark scenarios`)
- Branch: `codex/private-demo-benchmarks-d10`
- Final status after the implementation commit was clean; this report is
  committed separately as the sole follow-up change.

## Preserved starting state

The required base had exactly four modified files. They were inspected and
incorporated without reset or discard:

- `agenten/agent_factory/input_contracts.py`
- `agenten/agent_factory/input_document.py`
- `tests/agent_factory/test_input_document.py`
- `tests/agent_factory/test_private_benchmark_catalog.py`

## RED evidence

1. Initial focused run:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_document.py tests/agent_factory/test_private_benchmark_catalog.py
   ```

   Result: `39 failed, 54 passed`, exit 1. The failures exposed incomplete
   rejection of reserved nested headings, public policy prose being parsed as
   private scenario records, missing declared-tool authority, and invented
   scenario transformations and tool names.

2. Intermediate focused run after narrowing parser and generator authority:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_document.py tests/agent_factory/test_private_benchmark_catalog.py
   ```

   Result: `5 failed, 84 passed`, exit 1. Four failures showed the missing
   compiler binding for `tool_*` source assertions. The fifth exposed a stale
   Sales expectation that forced a human handoff from a Captain-only bounded
   attempt source.

3. New heading prescan cases before their production fix:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_document.py -k "indented_commonmark or semantically_hidden"
   ```

   Result: `7 failed, 51 deselected`, exit 1. CommonMark-indented headings and
   the hidden aliases `confidential`, `secret`, `internal`, and `nonpublic`
   were still admitted.

4. First complete non-live regression run:

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q -m "not live"
   ```

   Result: `1 failed, 3390 passed, 136 skipped, 21 deselected`, exit 1. A
   downstream test executor still emitted implicit tool receipts instead of
   receipts for the publicly declared required tools.

## GREEN gates

- Heading prescan target: `7 passed, 51 deselected`.
- Final focused private-input/catalog gate: `96 passed`.
- Downstream generic benchmark regression target: `1 passed`.
- Agent Factory plus relevant Gateway/runtime regression gate: `1538 passed`.
- Deterministic Hermes/Gateway integration: `7 passed, 1 skipped`; the skipped
  test was the separately authorized MariaDB-backed path.
- Architecture/import/workstream gate: `18 passed`.
- `python -m compileall -q agenten blockchain chats config gateway`: exit 0.
- `scripts/verify_submission.py`: `Submission evidence check passed.`, exit 0.
- `git diff --check`: exit 0 before the implementation commit.
- Added-line credential scan: exit 0; no credential-shaped assignment found.
- Coverage cleanup check: zero `.coverage.*` files remained after the gate.

## Complete non-live gate

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not live"
```

Result: `3391 passed, 136 skipped, 21 deselected`, coverage `82.33%`, exit 0.
One pre-existing Pydantic serializer warning was emitted by
`test_projection_adapter_uses_gateway_event_and_minibook_acknowledgement`; it
was not a test failure.

## Exact implementation scope

The implementation commit changed exactly these seven product/test files:

- `agenten/agent_factory/input_compiler.py`
- `agenten/agent_factory/input_contracts.py`
- `agenten/agent_factory/input_document.py`
- `agenten/agent_factory/private_benchmark_catalog.py`
- `tests/agent_factory/test_input_document.py`
- `tests/agent_factory/test_private_benchmark_catalog.py`
- `tests/gateway/test_capability_assembly_production.py`

The parser now prescans normalized Markdown headings and rejects private or
semantically hidden benchmark/holdout declarations while preserving the two
exact public policy headings. The 45 templates bind setup, action, predicate,
safety, tool receipt, and necessary handoff behavior to canonical public
sources. Required tools are typed public declarations, compiled to admitted
assertions, and enforced during generation. Catalog confinement, atomic
no-overwrite publication, repository/worktree exclusion, and the public/private
projection boundary remain enforced.

No canonical `TO_BE_BUILT.md` file was changed. This report is the only file in
the separate documentation commit.

## No-live boundary and nonclaims

- No live service, provider, Docker, MariaDB, n8n, Minibook, browser, or
  network call was run.
- The skipped MariaDB integration is not passing live evidence.
- No production deployment, credential change, push, merge, rebase, or branch
  mutation was performed.
- Synthetic private benchmarks are release-validation fixtures, not evidence
  of regulated-domain accuracy or production fitness.
- A clean `git status --short --branch` was recorded after the implementation
  commit. After committing this report, the same command must again show only
  the branch header before handoff.

---

## Review Fix Round 1

### Base and fix commit

- Review-fix starting commit: `2ed1460bc6fba60938be7f9eac16478d84cc9d35`
- Review-fix implementation commit: `f60e3bcd5a066f769d7aa689c71d9476d00a5607`
  (`fix(factory): harden private benchmark authority`)
- The implementation commit contains exactly eight product/test files. This
  report append is committed separately.

### Behavioral RED evidence

The four findings were reproduced before their production fixes with focused
`--no-cov` commands:

1. Semantic private-heading alias matrix and parser-effect counter:
   `1 failed`. `Private expected` reached `_level_sections` and failed there,
   proving the prescan did not reject the heading before parser effects.
2. Exact declared-tool binding across all three canonical inputs:
   `3 failed`. Generated private cases had no `declared_tool_keys` contract.
3. Independent Gateway safety and rationale evaluation:
   `3 failed`. Both `crm_writer` and the cross-swapped pair
   `account_snapshot_reader` plus `policy_lookup` were reported safe, while the
   canonical public outcome rationale was rejected in favor of the hidden
   `selective_routing` key.
4. Default writer confinement under a fake repository:
   `1 failed`. The direct caller did not reject the repository boundary before
   creating the private catalog.

A supplemental fail-closed Gateway binding test then reproduced two additional
contract drifts: `2 failed`. The builder accepted both an injected
`crm_writer` declaration and a rationale predicate changed back to the hidden
case key.

### Minimal fixes and scope

- The normalized prescan now rejects every level-two through level-six matrix
  combination of `private`, `confidential`, or `hidden` with `expected`,
  `result`, `outcome`, `answer`, `predicate`, `benchmark`, `holdout`,
  `evaluation`, `case`, or `fixture`. The existing exact level-two public
  policy headings remain the only exceptions, and rejection occurs before the
  section parser is called.
- Every private case binds the exact sorted public declared-tool set into the
  digest-covered holdout and evaluator-only view. Gateway assembly rechecks it
  against the admitted public input, and the evaluator marks any observed tool
  outside that set unsafe. Required-tool receipts remain independently
  enforced.
- Generated rationale predicates now bind the exact canonical public source
  outcome to the `business_outcome` observation. Gateway assembly rejects a
  hidden-key rationale contract, and evaluation requires the source-grounded
  value in both the observation and rationale text.
- The catalog writer no longer defaults to an empty forbidden-root set. It
  requires non-empty explicit verified boundaries or derives current
  repository/worktree and local Git-marker boundaries before creating the
  manifest bytes or any directory. A fake-repository direct caller leaves the
  requested private root absent and writes zero catalog bytes.
- The 45 explicit scenarios, declared required tools, atomic compiler batch,
  holdout digests, no-overwrite behavior, and D11-facing digest seams remain
  deterministic and covered by focused, broad, and integration gates.

Files in the implementation commit:

- `agenten/agent_factory/input_compiler.py`
- `agenten/agent_factory/input_contracts.py`
- `agenten/agent_factory/input_document.py`
- `agenten/agent_factory/private_benchmark_catalog.py`
- `gateway/capability_generic_benchmark.py`
- `tests/agent_factory/test_input_document.py`
- `tests/agent_factory/test_private_benchmark_catalog.py`
- `tests/gateway/test_capability_assembly_production.py`

### GREEN and verification evidence

- New finding-specific groups: `1 passed`, `3 passed`, `3 passed`, and
  `1 passed`.
- Supplemental public-authority drift group: `2 passed`; combined evaluator
  group: `5 passed`.
- Complete focused input/catalog/Gateway gate: `149 passed in 10.58s`.
- Relevant Agent Factory/Gateway/runtime gate: `1591 passed in 58.93s`.
- A prior run of that same gate had one transient pre-existing Windows
  concurrency failure in
  `test_filesystem_store_handles_concurrent_identical_replays` with
  `1590 passed`; the isolated test immediately passed (`1 passed in 0.54s`),
  no out-of-scope code was changed, and the complete relevant gate then passed.
- Deterministic Hermes/Gateway integration: `7 passed, 1 skipped in 1.82s`.
  The skip is the separately authorized MariaDB-backed case.
- Architecture/import/workstream gate: `18 passed in 2.16s`.
- `python -m compileall -q agenten blockchain chats config gateway`: exit 0.
- `scripts/verify_submission.py`: `Submission evidence check passed.`, exit 0.
- `git diff --check`: exit 0 before the fix commit.
- Case-sensitive added-line credential-assignment plus generic bearer/private-
  key/credential-URL scan: passed. An initial case-insensitive development
  pattern falsely matched ordinary names such as `observed_key`; it was
  corrected before recording the credential result.
- No `.coverage.*` artifact was produced by these `--no-cov` test runs.
- The six-minute full non-live suite was not rerun for this bounded review fix;
  the required focused and relevant broad gates are the evidence claimed here.

### Review-fix nonclaims

- No live service, database, provider, Docker, n8n, Minibook, browser, or
  network operation was run.
- The MariaDB skip is not live evidence.
- No expected predicate or expected answer was added to the executor case view,
  public projection, worker input, log, or public evidence record. Public tool
  names remain public; their evaluator binding remains inside private holdouts.
- No deployment, push, merge, rebase, credential change, or unrelated
  concurrency fix was performed.
- After the implementation commit, `git status --short --branch` showed only
  `## codex/private-demo-benchmarks-d10`. The same clean-status check is required
  after the separate report commit.

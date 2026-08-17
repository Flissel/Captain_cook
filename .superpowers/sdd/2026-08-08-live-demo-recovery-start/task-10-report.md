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

# Captain Cook Maintenance Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the repository's automated quality gate, close two correctness defects, give the newest feature a production caller, and make the ledger's audit claim match what the code actually guarantees.

**Architecture:** Six independent tasks in dependency order. Task 1 repairs CI (which has been structurally unable to pass since 2026-07-20) because every later task depends on a working gate. Task 2 lands the stalled draft PR. Tasks 3–5 are small, test-first code changes. Task 6 is an integration decision about the VibeMind submodule pointer.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio + pytest-cov, Pydantic v2, FastAPI, AutoGen 0.7.5, MariaDB, GitHub Actions.

## Global Constraints

- Python **3.11** and the project `.venv`. Dev deps: `requirements-dev.txt`.
- **TDD is mandatory**: add a failing acceptance test before any behavioral change (`AGENTS.md`).
- **Conventional Commits**, narrow scope per commit.
- **Do not commit directly to `main`.** This plan explicitly authorizes creating the branches named in each task; it authorizes no other branch creation, deletion, rebase, or force-push.
- `pytest.ini` sets `addopts = -m "not live and not legacy" --cov=agenten --cov=blockchain --cov=./gateway --cov-fail-under=70`. For focused runs use `-o addopts=""`; for CI-shaped runs use `--no-cov`.
- Tests marked `live` never run implicitly and **fail rather than skip** when selected.
- **Never rewrite `artifacts/demo-run.json`** unless the task says so. No task here does.
- Secrets belong only in gitignored `.env`. Never print or commit them.
- `load_adapter_bundle` resolves `entry.source_path` **relative to the process CWD**. Task 4 changes this; until then, assume repo-root CWD.
- **Current repo state:** the `captain_cook` submodule is at a **detached HEAD** on `origin/claude/repo-overview-r12ppe` (`3290eff`). Every task below starts by checking out a named branch, so no work is created on a detached HEAD.

---

### Task 1: Revive the Windows deterministic CI gate

**Why:** `deterministic_windows` requires `runs-on: [self-hosted, windows]`. `gh api repos/Flissel/Captain_cook/actions/runners` returns `total_count: 0`. Every run since 2026-07-20 waits 24h for a runner that never appears and is then cancelled — 12 cancelled / 13 failed / 2 success across the last 60 runs. The repo is **public**, so GitHub-hosted `windows-latest` minutes are free. This job runs only offline tests (`-m "not live" --ignore=tests/live`), so it does not need a local machine. `gate_e_live` stays self-hosted — it genuinely needs local n8n, Minibook, MariaDB and real secrets.

**Files:**
- Modify: `.github/workflows/gateway-and-gate-e.yml:58-73`
- Test: `tests/scripts/test_ci_workflow.py:58`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: a green `deterministic_windows` job on every push and pull request. Task 2 depends on this being green.

- [ ] **Step 1: Create the working branch from `origin/main`**

The CI file on `main` is the one that runs for every branch's checks, and this fix must be independent of the draft PR's content.

```bash
cd vibemind-os/spaces/captain_cook
git fetch origin
git checkout -b fix/ci-windows-hosted-runner origin/main
```

- [ ] **Step 2: Update the enforcing test to expect a GitHub-hosted runner**

`tests/scripts/test_ci_workflow.py` asserts the current value, so the test must change first — this is the failing test for this task.

In `tests/scripts/test_ci_workflow.py`, in `test_windows_ci_covers_the_full_deterministic_runtime`, replace line 58:

```python
    assert job["runs-on"] == ["self-hosted", "windows"]
```

with:

```python
    assert job["runs-on"] == "windows-latest"
```

Leave `test_gate_e_is_manual_and_uses_the_isolated_local_live_runner` (line 86) unchanged — Gate E stays self-hosted.

- [ ] **Step 3: Run the test to verify it fails**

```bash
python -m pytest -q -o addopts="" tests/scripts/test_ci_workflow.py::test_windows_ci_covers_the_full_deterministic_runtime -v
```

Expected: FAIL with `AssertionError: assert ['self-hosted', 'windows'] == 'windows-latest'`

- [ ] **Step 4: Change the workflow job**

Replace the whole `deterministic_windows` job in `.github/workflows/gateway-and-gate-e.yml` with:

```yaml
  deterministic_windows:
    name: Windows deterministic runtime
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install Captain and Minibook dependencies
        shell: pwsh
        run: >-
          python --version;
          python -m pip install --upgrade pip;
          python -m pip install -r requirements.txt -r minibook/requirements.txt pytest pytest-cov pytest-asyncio
      - name: Exercise complete Windows deterministic suite
        shell: pwsh
        run: python -m pytest -q --no-cov -rs -m "not live" --ignore=tests/live
```

Two things deliberately preserved because the test asserts them: `actions/checkout@v4` must remain `steps[0]`, and `submodules: recursive` must remain. `actions/setup-python` is added at `steps[1]` — the "no setup-python" assertion applies only to `gate_e_live`.

- [ ] **Step 5: Run the test to verify it passes**

```bash
python -m pytest -q -o addopts="" tests/scripts/test_ci_workflow.py -v
```

Expected: 3 passed

- [ ] **Step 6: Verify the submodule checkout will work on a clean runner**

`submodules: recursive` fetches `hermes-agent`. Confirm the pinned commit is reachable on the public remote:

```bash
git ls-remote https://github.com/Flissel/hermes-agent.git | grep fcbdeca1ebedf1d1ee7f0199f4df7bf3f39619a0
```

Expected: two lines (`HEAD` and `refs/heads/main`). If this returns nothing, stop — the runner cannot check out the submodule and this task cannot succeed.

- [ ] **Step 7: Commit and push the branch**

```bash
git add .github/workflows/gateway-and-gate-e.yml tests/scripts/test_ci_workflow.py
git commit -m "fix(ci): run the deterministic Windows suite on a hosted runner

No self-hosted runner is registered, so every run since 2026-07-20 waited
24h and was cancelled. The job runs only offline tests, so a GitHub-hosted
windows-latest runner is sufficient. Gate E stays self-hosted."
git push -u origin fix/ci-windows-hosted-runner
```

- [ ] **Step 8: Watch the first real run and record the install duration**

```bash
gh run list --repo Flissel/Captain_cook --branch fix/ci-windows-hosted-runner --limit 3
gh run watch --repo Flissel/Captain_cook
```

Expected: `deterministic_windows` completes (pass or genuine test failure) instead of hanging.

**Known risk to measure here:** `requirements.txt` pins `sentence-transformers==5.6.0`, which pulls `torch` (~2.5 GB on Windows). If the install step exceeds **15 minutes**, record the measured duration in the PR description and open a follow-up to introduce a `requirements-ci.txt` that omits `sentence-transformers`, `selenium`, and `plotly` — none of which the offline suite imports. Do not do that inside this task; keep this change to one reviewable concern.

- [ ] **Step 9: Open and merge the PR**

```bash
gh pr create --repo Flissel/Captain_cook --base main \
  --title "fix(ci): run the deterministic Windows suite on a hosted runner" \
  --body "No self-hosted runner is registered (actions/runners total_count: 0), so deterministic_windows has waited 24h and been cancelled on every run since 2026-07-20. The job runs only offline tests, so windows-latest suffices. Gate E remains self-hosted."
```

Merge only once `deterministic_windows` and `mariadb_gateway` are both green. **If the deterministic suite now reports real test failures, that is a finding, not a blocker for this task** — record the failures, merge the CI repair (a visible red gate beats an invisible one), and open a separate issue per failure.

---

### Task 2: Resolve the stalled draft PR #23

**Why:** PR #23 (`claude/repo-overview-r12ppe`, opened 2026-08-11, DRAFT) carries 11 commits of real feature work — production authority assembly, the Gateway resume flow, the digest-pinned adapter bundle, and the TOCTOU-safe loader. It has almost certainly been waiting for a green check that Task 1 has now made possible. Tasks 4 depends on these files existing on `main`.

> **SCOPE CHANGED BY THE HUMAN PARTNER (2026-08-17).** PR #23 is to be **rebased and tested
> locally only**. Do **not** `git push`, do **not** `--force-with-lease`, do **not** `gh pr ready`,
> and do **not** merge. Steps 4 and 5 below are therefore out of scope; stop after Step 3 and
> report. The consequence is that PR #23's files never reach `main`, so **Task 4 cannot branch from
> `main` as written** — it branches from the locally rebased branch instead and also stays local.

**Files:**
- No file changes in this task. This is an integration task.

**Interfaces:**
- Consumes: a green CI from Task 1.
- Produces: on `main` — `agenten/agent_factory/authority_assembly_contracts.py`, `agenten/agent_factory/authority_adapter_bundle.py`, `agenten/agent_factory/single_open.py`, `gateway/authority_resume_api.py`, `gateway/authority_resume_contracts.py`, `gateway/authority_resume_store.py`, `config/authority-adapter-bundle.v1.json`. Task 4 modifies two of these.

- [ ] **Step 1: Rebase the PR branch onto the repaired main**

```bash
cd vibemind-os/spaces/captain_cook
git fetch origin
git checkout -b claude/repo-overview-r12ppe origin/claude/repo-overview-r12ppe
git rebase origin/main
```

If the rebase conflicts in `.github/workflows/gateway-and-gate-e.yml` or `tests/scripts/test_ci_workflow.py`, **take main's version** of both — those are Task 1's deliverable.

- [ ] **Step 2: Re-pin the adapter bundle if any pinned source file changed**

The bundle pins five files by SHA-256. A rebase that touched `gateway/app.py` or `agenten/orchestration/pipeline.py` invalidates the pin. Verify:

```bash
python -c "from pathlib import Path; from agenten.agent_factory.authority_adapter_bundle import load_adapter_bundle; load_adapter_bundle(Path('config/authority-adapter-bundle.v1.json')); print('bundle verifies')"
```

Expected: `bundle verifies`. If it raises `adapter digest mismatch for role <role>`, re-pin:

```bash
python -c "from pathlib import Path; from agenten.agent_factory.authority_adapter_bundle import BUNDLE_SOURCE_PATHS, pin_adapter_bundle, write_adapter_bundle; write_adapter_bundle(pin_adapter_bundle(BUNDLE_SOURCE_PATHS), Path('config/authority-adapter-bundle.v1.json'))"
```

The bundle file is pinned to LF by `.gitattributes` (commit `19d3934`) because CRLF conversion breaks the digest. Confirm the file still ends with exactly one `\n` and contains no `\r`:

```bash
python -c "b=open('config/authority-adapter-bundle.v1.json','rb').read(); print('CRLF present:', b'\r' in b); print('ends with single LF:', b.endswith(b'\n') and not b.endswith(b'\n\n'))"
```

Expected: `CRLF present: False`, `ends with single LF: True`

- [ ] **Step 3: Run the full offline suite locally**

```bash
python -m pytest -q --no-cov -rs -m "not live" --ignore=tests/live
```

Expected: all pass. Record any failure and fix it before proceeding.

- [ ] **Step 4: Push and mark the PR ready for review**

```bash
git push --force-with-lease origin claude/repo-overview-r12ppe
gh pr ready 23 --repo Flissel/Captain_cook
```

`--force-with-lease` (never plain `--force`) is required after the rebase and is authorized for this branch by this task only.

- [ ] **Step 5: Merge once green**

```bash
gh pr checks 23 --repo Flissel/Captain_cook
gh pr merge 23 --repo Flissel/Captain_cook --squash
```

**If the work turns out not to be wanted**, close the PR with a written reason instead of leaving it open — an indefinitely open draft is the state this task exists to end. In that case, skip Task 4 entirely.

---

### Task 3: Fix the same-batch duplicate blindness in the constitution gate

**Why:** `ConstitutionGatekeeper._collect_pending_descriptions` reads `ledger_query.blocks_in_stage(Stage.VALIDATING)`, but the Ledger Recorder only *enqueues* its write; its writer loop has not drained when the Gatekeeper runs inline on the same `InMemoryEventBus.publish` call. The VALIDATING set is therefore empty for siblings proposed in the same decomposition batch, and two byte-identical children both reach `done` with `rejection_reason: None`. The duplicate check only ever fires across batches.

**Files:**
- Modify: `agenten/constitution/gatekeeper.py:23` (imports), `:79-91` (`__init__`), `:148-161` (`_collect_pending_descriptions`), `:163-174` (`_accept`)
- Test: `tests/agenten/constitution/test_gatekeeper.py`

**Interfaces:**
- Consumes: `LedgerQuery.blocks_in_stage(stage)`, `Stage`, `_field_from_block(block, key)` — all already present in the module.
- Produces: no public API change. `ConstitutionGatekeeper.__init__` keeps its exact existing signature; the new state is private (`self._accepted_in_flight`). No other task depends on this.

- [ ] **Step 1: Create the working branch**

```bash
cd vibemind-os/spaces/captain_cook
git fetch origin
git checkout -b fix/gatekeeper-same-batch-duplicates origin/main
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/agenten/constitution/test_gatekeeper.py`:

```python
@pytest.mark.asyncio
async def test_duplicate_sibling_in_same_batch_is_rejected():
    """Two identical children proposed before the Recorder drains must not both pass.

    The Recorder only enqueues its VALIDATING write, so the ledger cannot see
    sibling one when sibling two is judged. The Gatekeeper must remember its
    own verdicts to close that window.
    """
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-1", description="Knead the dough for ten minutes.")
    )
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-2", description="Knead the dough for ten minutes.")
    )

    assert [event.subproblem_id for event in accepted.events] == ["sp-1"]
    assert [event.subproblem_id for event in rejected.events] == ["sp-2"]
    assert rejected.events[0].reason == "duplicate"


@pytest.mark.asyncio
async def test_in_flight_memory_is_released_once_the_ledger_sees_the_subproblem():
    """The in-process memory must not become a permanent duplicate ban.

    Once the ledger knows the subproblem, the ledger view is authoritative
    again and an identical description is allowed exactly as before.
    """
    bus, accepted, rejected = wire_bus()
    ledger = FakeLedgerQuery()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=ledger
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-1", description="Knead the dough for ten minutes.")
    )
    ledger.seed(
        Stage.DONE,
        FakeBlock(
            index=2,
            data={
                "subproblem_id": "sp-1",
                "root_problem_id": "root-1",
                "description": "Knead the dough for ten minutes.",
            },
        ),
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-2", description="Knead the dough for ten minutes.")
    )

    assert [event.subproblem_id for event in accepted.events] == ["sp-1", "sp-2"]
    assert rejected.events == []


@pytest.mark.asyncio
async def test_identical_descriptions_under_different_roots_are_both_accepted():
    """The in-flight memory must stay scoped to one root problem."""
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(
            subproblem_id="sp-1",
            root_problem_id="root-1",
            description="Knead the dough for ten minutes.",
        )
    )
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(
            subproblem_id="sp-2",
            root_problem_id="root-2",
            description="Knead the dough for ten minutes.",
        )
    )

    assert [event.subproblem_id for event in accepted.events] == ["sp-1", "sp-2"]
    assert rejected.events == []
```

- [ ] **Step 3: Run the tests to verify the first one fails**

```bash
python -m pytest -q -o addopts="" tests/agenten/constitution/test_gatekeeper.py -v -k "same_batch or in_flight_memory or different_roots"
```

Expected: `test_duplicate_sibling_in_same_batch_is_rejected` FAILS (`assert ['sp-1', 'sp-2'] == ['sp-1']`). The other two PASS already — they are the guardrails that stop the fix from over-correcting.

- [ ] **Step 4: Add the in-flight memory**

In `agenten/constitution/gatekeeper.py`, widen the typing import on line 23:

```python
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
```

In `__init__`, after `self.llm_timeout_seconds = llm_timeout_seconds`, add:

```python
        # Subproblems this gate accepted but the Recorder has not persisted yet.
        # Maps subproblem_id -> (root_problem_id, description).
        self._accepted_in_flight: Dict[str, Tuple[str, str]] = {}
```

- [ ] **Step 5: Prune and merge the memory when collecting pending descriptions**

Replace `_collect_pending_descriptions` in full:

```python
    def _collect_pending_descriptions(self, exclude_subproblem_id: str) -> List[Tuple[str, str]]:
        pending: List[Tuple[str, str]] = []
        try:
            blocks = self.ledger_query.blocks_in_stage(Stage.VALIDATING)
        except Exception:
            blocks = []
        for block in blocks:
            if _field_from_block(block, "subproblem_id") == exclude_subproblem_id:
                continue
            block_root_id = _field_from_block(block, "root_problem_id")
            block_description = _field_from_block(block, "description")
            if isinstance(block_root_id, str) and isinstance(block_description, str):
                pending.append((block_root_id, block_description))

        self._forget_persisted_acceptances()
        for subproblem_id, entry in self._accepted_in_flight.items():
            if subproblem_id == exclude_subproblem_id:
                continue
            pending.append(entry)
        return pending

    def _forget_persisted_acceptances(self) -> None:
        """Drop remembered verdicts the ledger has caught up on.

        The Recorder enqueues its VALIDATING write rather than performing it,
        so a sibling proposed in the same batch is invisible in that stage.
        This gate therefore remembers what it accepted until the ledger can
        see it, then hands authority back to the ledger view — otherwise the
        memory would become a permanent, unbounded duplicate ban.
        """
        if not self._accepted_in_flight:
            return
        persisted: Set[str] = set()
        for stage in Stage:
            try:
                blocks = self.ledger_query.blocks_in_stage(stage)
            except Exception:
                continue
            for block in blocks:
                subproblem_id = _field_from_block(block, "subproblem_id")
                if isinstance(subproblem_id, str):
                    persisted.add(subproblem_id)
        for subproblem_id in list(self._accepted_in_flight):
            if subproblem_id in persisted:
                del self._accepted_in_flight[subproblem_id]
```

- [ ] **Step 6: Record each acceptance**

In `_accept`, before the `await self.bus.publish(...)` line, add:

```python
        self._accepted_in_flight[event.subproblem_id] = (
            event.meta.root_problem_id,
            event.description,
        )
```

It must be recorded **before** the publish: `InMemoryEventBus.publish` delivers inline, so the downstream chain can re-enter this gate before `publish` returns.

- [ ] **Step 7: Run the tests to verify they pass**

```bash
python -m pytest -q -o addopts="" tests/agenten/constitution/test_gatekeeper.py -v
```

Expected: all pass, including the pre-existing tests.

- [ ] **Step 8: Verify no regression in the wider offline suite and the demo**

```bash
python -m pytest -q --no-cov -rs -m "not live" --ignore=tests/live
python main.py demo --output .captain-cook/plan-check-demo.json
```

Expected: suite green; demo prints `Demo complete: 4 subproblems reached done`. The four demo subproblems have distinct descriptions, so none is a duplicate. **Write the demo output to `.captain-cook/`, never to `artifacts/demo-run.json`.**

- [ ] **Step 9: Commit and open the PR**

```bash
git add agenten/constitution/gatekeeper.py tests/agenten/constitution/test_gatekeeper.py
git commit -m "fix(constitution): catch duplicate siblings within one batch

The gate read VALIDATING-stage blocks, but the Recorder only enqueues that
write, so same-batch siblings were invisible and two identical subproblems
both reached done. The gate now remembers its own verdicts until the ledger
can see them, then defers to the ledger view again."
git push -u origin fix/gatekeeper-same-batch-duplicates
gh pr create --repo Flissel/Captain_cook --base main \
  --title "fix(constitution): catch duplicate siblings within one batch" \
  --body "Closes the window in which ConstitutionGatekeeper could not see siblings proposed in the same decomposition batch. Adds three tests covering the fix, the memory release, and root-scoping."
```

---

### Task 4: Give the digest-pinned adapter bundle a production caller

**Why:** `pin_adapter_bundle` and `load_adapter_bundle` have **zero non-test callers**. Meanwhile `gateway/authority_resume_api.py::authorize` issues a resume authorization for any well-formed 64-hex `assembly_id` without checking that the authority code still matches its pin. That is a fail-open step inside a flow named "fail-closed". Wiring the bundle verification into `authorize` closes the hole and gives the newest feature a reason to exist.

> **SCOPE CHANGED BY THE HUMAN PARTNER (2026-08-17).** Because PR #23 stays local and unpushed,
> the files this task modifies never reach `main`. Branch from the **locally rebased PR #23 branch**
> (`claude/repo-overview-r12ppe` after Task 2's rebase), not from `origin/main`. Commit locally;
> do **not** push and do **not** open a PR. Everything else in the task stands.

**Depends on:** Task 2 (these files only exist on `main` after PR #23 merges).

**Files:**
- Modify: `agenten/agent_factory/authority_adapter_bundle.py:115-147` (add `root` parameter)
- Modify: `gateway/authority_resume_api.py:1-52`
- Test: `tests/agent_factory/test_authority_adapter_bundle.py`, `tests/gateway/test_authority_resume_bundle_guard.py` (create)

**Do not add the guard test to `tests/gateway/test_authority_resume_flow.py`.** That whole file carries `pytestmark = pytest.mark.skipif(not TEST_MARIADB_DSN, ...)`, so a test placed there would be silently skipped in the deterministic Windows suite — exactly the kind of invisible gate this plan exists to remove. The guard needs no database, so it gets its own MariaDB-free file.

**Interfaces:**
- Consumes: `load_adapter_bundle(source: Path, *, root: Path | None = None) -> tuple[AuthorityAdapterBundleV1, str]` and `AuthorityAdapterBundleError` from `agenten.agent_factory.authority_adapter_bundle`.
- Produces: `build_authority_resume_router(store_provider, *, clock=None, verify_authority_bundle: Callable[[], None] | None = None)`. The new keyword-only parameter defaults to `None`, which selects the repo-root bundle verifier, so existing callers of `build_authority_resume_router` need no change.

- [ ] **Step 1: Create the working branch**

```bash
cd vibemind-os/spaces/captain_cook
git fetch origin
git checkout -b feat/authority-bundle-resume-guard origin/main
```

- [ ] **Step 2: Write the failing test for root-relative bundle loading**

`load_adapter_bundle` currently resolves `entry.source_path` against the process CWD, so a Gateway started from any other directory would fail to verify. Append to `tests/agent_factory/test_authority_adapter_bundle.py`:

```python
def test_load_adapter_bundle_resolves_sources_against_an_explicit_root(tmp_path):
    """The Gateway must verify the bundle regardless of its working directory."""
    from pathlib import Path

    from agenten.agent_factory.authority_adapter_bundle import (
        BUNDLE_SOURCE_PATHS,
        load_adapter_bundle,
    )

    repo_root = Path(__file__).resolve().parents[2]
    bundle_path = repo_root / "config" / "authority-adapter-bundle.v1.json"

    bundle, digest = load_adapter_bundle(bundle_path, root=repo_root)

    assert len(bundle.adapters) == len(BUNDLE_SOURCE_PATHS)
    assert len(digest) == 64
```

- [ ] **Step 3: Run it to verify it fails**

```bash
python -m pytest -q -o addopts="" tests/agent_factory/test_authority_adapter_bundle.py::test_load_adapter_bundle_resolves_sources_against_an_explicit_root -v
```

Expected: FAIL with `TypeError: load_adapter_bundle() got an unexpected keyword argument 'root'`

- [ ] **Step 4: Add the `root` parameter**

In `agenten/agent_factory/authority_adapter_bundle.py`, change the signature and the per-entry path resolution:

```python
def load_adapter_bundle(
    source: Path,
    *,
    root: Path | None = None,
) -> tuple[AuthorityAdapterBundleV1, str]:
```

and inside the `for entry in bundle.adapters:` loop, replace `source_path = Path(entry.source_path)` with:

```python
        source_path = Path(entry.source_path)
        if root is not None:
            source_path = root / source_path
```

Everything else in the function stays as it is.

- [ ] **Step 5: Run it to verify it passes**

```bash
python -m pytest -q -o addopts="" tests/agent_factory/test_authority_adapter_bundle.py -v
```

Expected: all pass, including the pre-existing bundle tests.

- [ ] **Step 6: Write the failing test for the authorize guard**

Create `tests/gateway/test_authority_resume_bundle_guard.py`. It stubs the store, so it needs no MariaDB and runs in the ordinary offline suite:

```python
"""The authorize step must fail closed when the pinned authority bundle drifts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agenten.agent_factory.authority_adapter_bundle import AuthorityAdapterBundleError
from gateway.auth import GatewayRole, require_captain
from gateway.authority_resume_api import build_authority_resume_router

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
ASSEMBLY = "a" * 64


class _StubRecord:
    def __init__(self) -> None:
        self.authorization_id = uuid4()
        self.expires_at = NOW + timedelta(minutes=10)


class _StubStore:
    """Records whether the router reached the store at all."""

    def __init__(self) -> None:
        self.authorize_calls = 0

    def authorize(self, assembly_id: str, *, now: datetime):
        self.authorize_calls += 1
        return _StubRecord(), "raw-token"


def _client(verifier, store: _StubStore) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_authority_resume_router(
            lambda: store,
            clock=lambda: NOW,
            verify_authority_bundle=verifier,
        )
    )
    app.dependency_overrides[require_captain] = lambda: GatewayRole.CAPTAIN
    return TestClient(app)


def test_authorize_is_denied_when_the_bundle_does_not_verify():
    def refuse() -> None:
        raise AuthorityAdapterBundleError("adapter digest mismatch for role gateway")

    store = _StubStore()
    response = _client(refuse, store).post(
        f"/v1/authority/assemblies/{ASSEMBLY}/resume-authorizations"
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "authority_bundle_unverified"
    assert "digest mismatch" not in response.text
    assert store.authorize_calls == 0


def test_authorize_proceeds_when_the_bundle_verifies():
    store = _StubStore()
    response = _client(lambda: None, store).post(
        f"/v1/authority/assemblies/{ASSEMBLY}/resume-authorizations"
    )

    assert response.status_code == 201
    assert response.json()["token"] == "raw-token"
    assert store.authorize_calls == 1


def test_the_default_verifier_accepts_the_committed_bundle():
    """The shipped pin must actually verify, or the guard bricks every resume."""
    from gateway.authority_resume_api import _verify_pinned_authority_bundle

    _verify_pinned_authority_bundle()
```

The third test is the one that matters most operationally: it fails the build if the committed bundle ever drifts from the five source files, which is exactly the drift the pin exists to catch.

- [ ] **Step 7: Run it to verify it fails**

```bash
python -m pytest -q -o addopts="" tests/gateway/test_authority_resume_bundle_guard.py -v
```

Expected: FAIL — `build_authority_resume_router()` has no `verify_authority_bundle` parameter yet, and `_verify_pinned_authority_bundle` does not exist.

- [ ] **Step 8: Add the guard to the router**

In `gateway/authority_resume_api.py`, add to the imports:

```python
from pathlib import Path

from agenten.agent_factory.authority_adapter_bundle import (
    AuthorityAdapterBundleError,
    load_adapter_bundle,
)
```

Add below `_DENIAL_STATUS`:

```python
REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = REPO_ROOT / "config" / "authority-adapter-bundle.v1.json"


def _verify_pinned_authority_bundle() -> None:
    """Raise AuthorityAdapterBundleError unless every adapter matches its digest.

    Resuming means handing authority back to code. If that code has drifted
    from the bundle it was pinned as, the only safe answer is no. This raises
    the domain error; the route converts it to a denial that names no role and
    echoes no content.
    """
    load_adapter_bundle(BUNDLE_PATH, root=REPO_ROOT)
```

The verifier deliberately raises the **domain** error rather than an `HTTPException`, so it stays a plain callable that tests and non-HTTP callers can use. The route owns the status-code mapping.

Change the factory signature to:

```python
def build_authority_resume_router(
    store_provider: Callable[[], AuthorityResumeStore],
    *,
    clock: Callable[[], datetime] | None = None,
    verify_authority_bundle: Callable[[], None] | None = None,
) -> APIRouter:
    router = APIRouter()
    now = clock or (lambda: datetime.now(timezone.utc))
    verify_bundle = verify_authority_bundle or _verify_pinned_authority_bundle
```

and in `authorize`, insert the guarded call directly after the id check:

```python
        _require_assembly_id(assembly_id)
        try:
            verify_bundle()
        except AuthorityAdapterBundleError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="authority_bundle_unverified",
            ) from None
        record, raw_token = store_provider().authorize(assembly_id, now=now())
```

`from None` suppresses the cause chain so the mismatched role never reaches the response or the traceback in a log.

Guard only `authorize`. `dispatch` and `readback` operate on an authorization that was already issued under a verified bundle; re-verifying there would let unrelated source edits invalidate an in-flight dispatch.

- [ ] **Step 9: Run the tests to verify they pass**

```bash
python -m pytest -q -o addopts="" tests/gateway/test_authority_resume_bundle_guard.py tests/agent_factory/ -v
```

Expected: all pass.

- [ ] **Step 10: Verify the import boundary still holds**

`gateway/` now imports from `agenten.agent_factory`. Confirm the architecture tests accept that direction:

```bash
python -m pytest -q -o addopts="" tests/test_architecture_fitness.py tests/test_import_boundaries.py -v
```

Expected: all pass. If a boundary test rejects `gateway -> agenten.agent_factory`, **stop and report** rather than editing the boundary test — that would be a real architectural decision, not a mechanical fix.

- [ ] **Step 11: Commit and open the PR**

```bash
git add agenten/agent_factory/authority_adapter_bundle.py gateway/authority_resume_api.py tests/agent_factory/test_authority_adapter_bundle.py tests/gateway/test_authority_resume_bundle_guard.py
git commit -m "feat(gateway): verify the pinned authority bundle before authorizing a resume

authorize() accepted any well-formed assembly id without checking that the
authority code still matched its digest pin. The pinned bundle now gates
that step, which also gives load_adapter_bundle its first production caller.
Bundle sources resolve against an explicit repo root so the check does not
depend on the Gateway's working directory."
git push -u origin feat/authority-bundle-resume-guard
gh pr create --repo Flissel/Captain_cook --base main \
  --title "feat(gateway): verify the pinned authority bundle before authorizing a resume" \
  --body "Closes the fail-open step in the fail-closed resume flow and gives the digest-pinned adapter bundle a production caller."
```

---

### Task 5: Make the ledger's integrity claim verifiable and the docstrings true

**Why:** `Block.compute_hash` covers only `index, block_type, data, previous_hash, parent_index` — `status` and `metadata` are outside the hash. No chain-verification routine exists anywhere in the repo, so `previous_hash` is written but never walked. Separately, `agenten/ledger_bridge/recorder.py` documents `update_task_status` as recomputing the hash; it does not. The MariaDB gateway path never mutates a block after insert, so it *can* be verified; the in-process pipeline ledger mutates `data` in place and cannot. Both facts should be stated and one of them enforced.

**Files:**
- Modify: `blockchain/Blockchain_modell.py` (add `ChainVerificationError` and `verify_chain`)
- Modify: `agenten/ledger_bridge/recorder.py` (correct the false docstring)
- Modify: `docs/ARCHITECTURE.md` (state the boundary)
- Test: `tests/blockchain/test_chain_verification.py` (create)

**Interfaces:**
- Consumes: `Block(index, block_type, data, status, previous_hash, parent_index=None, children=None, metadata=None, hash=None)` and `Block.compute_hash()`.
- Produces: `verify_chain(blocks: List[Block]) -> None`, raising `ChainVerificationError`. Both exported from `blockchain.Blockchain_modell`.

- [ ] **Step 1: Create the working branch**

```bash
cd vibemind-os/spaces/captain_cook
git fetch origin
git checkout -b feat/ledger-chain-verification origin/main
```

- [ ] **Step 2: Write the failing tests**

Create `tests/blockchain/test_chain_verification.py`:

```python
"""Chain verification for append-only, never-mutated ledgers."""
import pytest

from blockchain.Blockchain_modell import Block, ChainVerificationError, verify_chain


def _append(blocks, block_type, data, status="pending"):
    previous_hash = blocks[-1].hash if blocks else "0"
    block = Block(
        index=len(blocks),
        block_type=block_type,
        data=data,
        status=status,
        previous_hash=previous_hash,
    )
    blocks.append(block)
    return block


def test_verify_chain_accepts_an_untouched_append_only_chain():
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    _append(blocks, "batch_claimed", {"batch_id": "b-1"})
    _append(blocks, "recovery_decision", {"batch_id": "b-1", "decision": "requeue"})

    verify_chain(blocks)


def test_verify_chain_accepts_an_empty_chain():
    verify_chain([])


def test_verify_chain_detects_a_mutated_payload():
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    tampered = _append(blocks, "batch_claimed", {"batch_id": "b-1"})

    tampered.data["batch_id"] = "b-2"

    with pytest.raises(ChainVerificationError) as error:
        verify_chain(blocks)
    assert "index 1" in str(error.value)


def test_verify_chain_detects_a_broken_link():
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    _append(blocks, "batch_claimed", {"batch_id": "b-1"})

    blocks[1].previous_hash = "0" * 64

    with pytest.raises(ChainVerificationError):
        verify_chain(blocks)


def test_status_is_outside_the_hash_by_design():
    """Documents a real limitation: a status-only edit is NOT detected.

    `status` is deliberately excluded from compute_hash because the in-process
    pipeline updates it after append. Anyone reading verify_chain as full
    tamper-evidence needs to see this boundary spelled out.
    """
    blocks = []
    _append(blocks, "genesis", {}, status="completed")
    _append(blocks, "batch_claimed", {"batch_id": "b-1"}, status="pending")

    blocks[1].status = "aborted_infra"

    verify_chain(blocks)
```

- [ ] **Step 3: Run them to verify they fail**

```bash
python -m pytest -q -o addopts="" tests/blockchain/test_chain_verification.py -v
```

Expected: collection error — `ImportError: cannot import name 'ChainVerificationError'`

- [ ] **Step 4: Implement verification**

In `blockchain/Blockchain_modell.py`, after the imports and before `class Block`, add:

```python
class ChainVerificationError(ValueError):
    """Raised when a ledger chain fails hash or linkage verification."""
```

At module level, after `class Block`, add:

```python
def verify_chain(blocks: List[Block]) -> None:
    """Verify linkage and per-block hash integrity of an append-only chain.

    Valid only for chains whose blocks are never mutated after append — the
    MariaDB gateway path, which issues no UPDATE or DELETE against `blocks`.
    The in-process pipeline ledger mutates `data`, `status` and `metadata` in
    place after append and is expected to fail this check; do not call it there.

    `status`, `children` and `metadata` are outside `compute_hash`, so edits to
    those fields are not detected. This verifies payload and linkage only.
    """
    previous_hash = "0"
    for block in blocks:
        if block.previous_hash != previous_hash:
            raise ChainVerificationError(
                f"broken chain link at index {block.index}"
            )
        recomputed = block.compute_hash()
        if block.hash != recomputed:
            raise ChainVerificationError(
                f"payload hash mismatch at index {block.index}"
            )
        previous_hash = block.hash
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
python -m pytest -q -o addopts="" tests/blockchain/test_chain_verification.py -v
```

Expected: 5 passed

- [ ] **Step 6: Correct the false docstring in the recorder**

The claim lives in `_touch`'s docstring at `agenten/ledger_bridge/recorder.py:369-375`, not in the module docstring. Confirm the location first:

```bash
grep -n "recomputes hash" agenten/ledger_bridge/recorder.py
```

Expected: one hit at line 372. Replace the whole docstring so it reads:

```python
    def _touch(self, index: int) -> None:
        """Persist an in-place `.data`/`.metadata` mutation that did not
        change `.status`, by round-tripping through the public
        `update_task_status` with the block's current status unchanged.
        Keeps us inside `Blockchain`'s public API instead of reaching for
        its private `_save()`.

        `update_task_status` saves but does NOT recompute the block hash.
        Because `.data` IS inside `Block.compute_hash`, every mutation
        persisted here leaves the stored hash stale — by design, not by
        accident. That is why the in-process ledger is a single-writer
        append-only log with validated stage transitions rather than a
        hash-verifiable chain; see
        `blockchain.Blockchain_modell.verify_chain`, which is scoped to the
        gateway ledger and will fail against this one.
        """
```

Leave the two statements in the method body unchanged.

- [ ] **Step 7: State the boundary in the architecture doc**

Append to `docs/ARCHITECTURE.md`:

```markdown
### Ledger integrity boundary

Two ledgers with different guarantees share one `Block` type.

The MariaDB gateway ledger is append-only in the strong sense: `GatewayStore`
is the sole writer, issues no `UPDATE` or `DELETE` against `blocks`, and
serializes appends via `SELECT ... FOR UPDATE` on the chain tip. It can be
checked with `blockchain.Blockchain_modell.verify_chain`.

The in-process pipeline ledger (`LedgerRecorderAgent`) mutates `data`,
`status`, `metadata` and `children` in place after append and does not
recompute hashes. Its integrity guarantee is single-writer discipline plus
validated stage transitions, not hash verification. `verify_chain` is
expected to fail against it and must not be called there.

`status`, `children` and `metadata` are outside `Block.compute_hash` in both
cases, so neither ledger detects edits confined to those fields.
```

- [ ] **Step 8: Run the full offline suite**

```bash
python -m pytest -q --no-cov -rs -m "not live" --ignore=tests/live
```

Expected: all pass.

- [ ] **Step 9: Commit and open the PR**

```bash
git add blockchain/Blockchain_modell.py agenten/ledger_bridge/recorder.py docs/ARCHITECTURE.md tests/blockchain/test_chain_verification.py
git commit -m "feat(blockchain): add chain verification and correct the integrity claims

previous_hash was written but never walked, and the recorder docstring
claimed a hash recomputation that does not happen. Adds verify_chain for the
append-only gateway ledger and documents why the in-process ledger cannot
use it."
git push -u origin feat/ledger-chain-verification
gh pr create --repo Flissel/Captain_cook --base main \
  --title "feat(blockchain): add chain verification and correct the integrity claims" \
  --body "Makes the auditability claim checkable where it holds and explicit where it does not."
```

**Follow-up worth opening as an issue, not doing here:** call `verify_chain` from a `live`-marked MariaDB integration test so the gateway chain is actually verified against real stored rows. That needs a MariaDB fixture and belongs in its own task.

---

### Task 6: Decide and record Captain Cook's place in VibeMind

**Why:** `captain_cook` is a submodule of `vibemind-os` with **zero references anywhere else** — `git grep` over `vibemind-os` excluding the submodule directory returns only `.gitmodules`. Separately, the outer `Vibemind_V1` repo pins `vibemind-os` at `630452e`, which is **17 commits behind** the local `vibemind-os` HEAD `b6bb335` and therefore predates both Captain Cook commits. A fresh clone of the outer repo does not get `spaces/captain_cook` at all. This is a decision, not a refactor.

**Files:**
- Modify (option A only): the outer repo gitlink for `vibemind-os`
- Create: `docs/superpowers/plans/2026-08-17-vibemind-integration-decision.md`

**Interfaces:**
- Consumes: nothing. Independent of Tasks 1–5.
- Produces: a written decision. No code interface.

- [ ] **Step 1: Confirm the current drift**

```bash
cd /c/Users/User/Desktop/Vibemind_V1
git ls-tree HEAD vibemind-os
git -C vibemind-os log -1 --format="%h %s"
git -C vibemind-os rev-list --count 630452e2d1fa90e98aad40a44a52190a65485c1b..HEAD
git -C vibemind-os status --short
```

Record the four outputs. The last one matters most: `vibemind-os` has pre-existing uncommitted work from other sessions (deleted `tests/legacy-brain/` files, modified submodule pointers) that is **not yours and must not be discarded**.

- [ ] **Step 2: Choose one option and write it down**

Create `docs/superpowers/plans/2026-08-17-vibemind-integration-decision.md` in `captain_cook` recording which option was chosen and why, then act on it.

**Option A — Captain Cook is a real VibeMind space.** Then the outer gitlink must be bumped so fresh clones get it. Bump only the gitlink, in its own commit, touching nothing else:

```bash
cd /c/Users/User/Desktop/Vibemind_V1
git add vibemind-os
git status --short
```

Confirm `git status --short` shows **only** `M vibemind-os` staged before committing. If anything else is staged, unstage it — the outer repo has concurrent work from other agent sessions.

```bash
git commit -m "chore(submodule): bump vibemind-os gitlink to include spaces/captain_cook"
```

Then open a follow-up task to actually wire it — a space registry entry, a launcher preset, or an OpenFang agent — because a submodule nobody imports is not an integration.

**Option B — Captain Cook is a standalone project.** Then it does not need to be a VibeMind submodule. Record that decision, leave the outer gitlink alone, and work on Captain Cook directly in its own clone at `C:\Users\User\Desktop\Captain_cook`. Note in the decision file that the copy under `vibemind-os/spaces/` is then a convenience checkout, not the source of truth.

- [ ] **Step 3: Restore the submodule to its pinned commit if you are done experimenting**

This plan's investigation left the submodule on a detached checkout of the draft PR branch. Once Task 2 has merged, return it to a named branch:

```bash
cd vibemind-os/spaces/captain_cook
git fetch origin
git checkout main
git pull --ff-only origin main
```

If Task 2 was abandoned instead, restore the original pin:

```bash
git checkout 644a5c3b827ea8345c29a5256425117f8e117cc4
```

- [ ] **Step 4: Update the WORKBOARD**

Add a dated row to `WORKBOARD.md` in the outer repo recording what was decided and what changed, following the format of the existing rows. Commit it immediately — the board is the live lock other concurrent sessions read.

---

### Task 7: Make CI install the dependency set the repo declares

**Why:** Discovered while executing Task 1. The workflow installs `pytest pytest-cov pytest-asyncio`
**unpinned** alongside `requirements.txt`, and CI resolved `pytest 9.1.1` / `pytest-cov 7.1.0`.
The repo pins `pytest==9.0.2` / `pytest-cov==4.1.0` / `pytest-asyncio==1.4.0` in
`requirements-dev.txt`. So CI has never tested the dependency set the project actually declares,
and a breaking release of any of the three lands in CI unannounced. `requirements-dev.txt` already
composes exactly the right set: `-r requirements.txt`, `-r minibook/requirements.txt`, plus the
three pinned test packages.

**Files:**
- Modify: `.github/workflows/gateway-and-gate-e.yml` (the install step of both `mariadb_gateway`
  and `deterministic_windows`)
- Test: `tests/scripts/test_ci_workflow.py`

**Interfaces:**
- Consumes: Task 1's merged workflow.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Create the working branch**

```bash
cd <worktree>
git fetch origin
git checkout -b fix/ci-pinned-test-dependencies origin/main
```

- [ ] **Step 2: Update the assertions first**

`tests/scripts/test_ci_workflow.py` asserts `"pytest-asyncio" in commands` for both jobs. Tighten
both to require the pinned file instead. In `test_gateway_ci_uses_a_real_mariadb_service_without_skips`
and in `test_windows_ci_covers_the_full_deterministic_runtime`, replace:

```python
    assert "pytest-asyncio" in commands
```

with:

```python
    assert "requirements-dev.txt" in commands
    assert "pytest-asyncio" not in commands
```

- [ ] **Step 3: Run to verify it fails**

```bash
python -m pytest -q -o addopts="" tests/scripts/test_ci_workflow.py -v
```

Expected: both tests FAIL — the workflow still names the loose packages.

- [ ] **Step 4: Change both install steps**

In `mariadb_gateway`, replace the install run with:

```yaml
      - name: Install Captain dependencies
        run: python -m pip install --upgrade pip && python -m pip install -r requirements-dev.txt
```

In `deterministic_windows`, replace the install run with:

```yaml
      - name: Install Captain and Minibook dependencies
        shell: pwsh
        run: >-
          python --version;
          python -m pip install --upgrade pip;
          python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` already pulls `minibook/requirements.txt`, so dropping the explicit
`-r minibook/requirements.txt` loses nothing.

- [ ] **Step 5: Run to verify it passes**

```bash
python -m pytest -q -o addopts="" tests/scripts/test_ci_workflow.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit, push, and confirm CI still green**

```bash
git add .github/workflows/gateway-and-gate-e.yml tests/scripts/test_ci_workflow.py
git commit -m "fix(ci): install the pinned dependency set instead of loose test packages

CI resolved pytest 9.1.1 while the repo pins 9.0.2, so the suite never ran
against the declared dependency set. requirements-dev.txt already composes
runtime + minibook + pinned test packages."
git push -u origin fix/ci-pinned-test-dependencies
gh pr create --repo Flissel/Captain_cook --base main \
  --title "fix(ci): install the pinned dependency set instead of loose test packages" \
  --body "CI installed pytest/pytest-cov/pytest-asyncio unpinned and resolved pytest 9.1.1 while the repo pins 9.0.2. Switches both jobs to requirements-dev.txt."
```

Watch the run. A version change can surface new failures; if it does, that is the point of the
task — record them and decide per finding, do not loosen the pins back.

---

### Task 8: Bound the gatekeeper's in-flight memory and drop the whole-ledger prune scan

**Why:** Both defects are in **Task 3's design as this plan prescribed it** — found by the Task 3
reviewer, confirmed against the code, not implementer error. Task 3 still lands, because it fixes a
real hole (siblings in one batch were never duplicate-checked at all). This task repairs the repair.

1. **Orphaned entries are permanent.** `_forget_persisted_acceptances` prunes an entry only when its
   `subproblem_id` appears in some ledger stage. But `LedgerRecorderAgent._apply_subproblem_accepted`
   (`agenten/ledger_bridge/recorder.py:685-688`) silently returns without writing when
   `_require_validating_block` finds no VALIDATING block, and the writer loop
   (`recorder.py:327-353`) swallows any apply-step exception and continues. In that case the entry is
   never pruned, and `ConstitutionGatekeeper` is a long-lived pipeline component
   (`agenten/orchestration/pipeline.py:324-327`, built once). A later, genuinely distinct subproblem
   with the same normalized description under the same root is then rejected as `duplicate` forever.
2. **The prune scan is O(all history) on the hot path.** The helper iterates all 10 `Stage` members
   and calls `blocks_in_stage` for each, whenever the map is non-empty — precisely during the batch
   fan-out this feature exists to cover. Terminal buckets (`DONE`/`FAILED`/`REJECTED`) are filled by
   `_index_move` (`agenten/ledger_bridge/query.py:124-127`) and never emptied, so the scan grows with
   total historical subproblem count. `query.py:1-10` explicitly promises the read side "never scans
   the whole chain" — this defeats that promise.

**Fix both at once by removing the ledger scan entirely.** The memory only has to cover the window
between a verdict and the Recorder draining, which is one decomposition batch. A small bounded,
insertion-ordered map gives that with no ledger access, no unbounded growth, and no orphan path.

**Files:**
- Modify: `agenten/constitution/gatekeeper.py`
- Modify: `agenten/constitution/validators.py` (stale docstring, see Step 5)
- Test: `tests/agenten/constitution/test_gatekeeper.py`

**Interfaces:**
- Consumes: Task 3's `_accepted_in_flight` and `_forget_persisted_acceptances`.
- Produces: no public API change. `ConstitutionGatekeeper.__init__` keeps its signature.

- [ ] **Step 1: Create the working branch**

```bash
cd <worktree>
git fetch origin
git checkout -b fix/gatekeeper-bounded-in-flight-memory origin/main
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/agenten/constitution/test_gatekeeper.py`:

```python
@pytest.mark.asyncio
async def test_in_flight_memory_is_bounded_and_evicts_oldest_first():
    """The memory must not grow without bound when the ledger never catches up.

    A Recorder that fails to persist an acceptance would otherwise leave an
    entry in place for the life of the process, permanently banning that
    description under that root.
    """
    bus, accepted, rejected = wire_bus()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=FakeLedgerQuery()
    )

    for index in range(gatekeeper.IN_FLIGHT_MEMORY_LIMIT + 1):
        await gatekeeper.handle_subproblem_proposed(
            make_proposed(
                subproblem_id=f"sp-{index}",
                description=f"Prepare baking step number {index}.",
            )
        )

    assert len(gatekeeper._accepted_in_flight) == gatekeeper.IN_FLIGHT_MEMORY_LIMIT
    assert "sp-0" not in gatekeeper._accepted_in_flight
    assert rejected.events == []


@pytest.mark.asyncio
async def test_prune_does_not_query_the_ledger():
    """Pruning must not scan the ledger: it ran on every proposal during a batch."""
    bus, accepted, rejected = wire_bus()

    class CountingLedgerQuery(FakeLedgerQuery):
        def __init__(self):
            super().__init__()
            self.stage_queries = 0

        def blocks_in_stage(self, stage):
            self.stage_queries += 1
            return super().blocks_in_stage(stage)

    ledger = CountingLedgerQuery()
    gatekeeper = ConstitutionGatekeeper(
        bus=bus, ruleset=make_ruleset(), ledger_query=ledger
    )

    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-1", description="Knead the dough for ten minutes.")
    )
    first = ledger.stage_queries
    await gatekeeper.handle_subproblem_proposed(
        make_proposed(subproblem_id="sp-2", description="Shape the loaf and score it.")
    )

    # Only the single VALIDATING lookup per proposal, never a sweep of all stages.
    assert ledger.stage_queries - first == 1
```

- [ ] **Step 3: Run to verify they fail**

```bash
python -m pytest -q -o addopts="" tests/agenten/constitution/test_gatekeeper.py -v -k "bounded or does_not_query"
```

Expected: both FAIL — `IN_FLIGHT_MEMORY_LIMIT` does not exist, and the current pruner sweeps all
ten stages, so the delta is 10 or 11 rather than 1.

- [ ] **Step 4: Replace the memory with a bounded map and delete the scan**

In `agenten/constitution/gatekeeper.py`, add the class attribute inside `ConstitutionGatekeeper`,
above `__init__`:

```python
    # The in-flight memory only has to span the window between this gate's
    # verdict and the Recorder draining its queue — one decomposition batch.
    # A hard cap keeps a Recorder that never persists an acceptance from
    # turning a remembered verdict into a permanent ban on that description.
    IN_FLIGHT_MEMORY_LIMIT = 256
```

Delete `_forget_persisted_acceptances` entirely, and remove its call from
`_collect_pending_descriptions`, leaving:

```python
    def _collect_pending_descriptions(self, exclude_subproblem_id: str) -> List[Tuple[str, str]]:
        pending: List[Tuple[str, str]] = []
        try:
            blocks = self.ledger_query.blocks_in_stage(Stage.VALIDATING)
        except Exception:
            blocks = []
        for block in blocks:
            if _field_from_block(block, "subproblem_id") == exclude_subproblem_id:
                continue
            block_root_id = _field_from_block(block, "root_problem_id")
            block_description = _field_from_block(block, "description")
            if isinstance(block_root_id, str) and isinstance(block_description, str):
                pending.append((block_root_id, block_description))

        for subproblem_id, entry in self._accepted_in_flight.items():
            if subproblem_id == exclude_subproblem_id:
                continue
            pending.append(entry)
        return pending
```

A remembered entry can now also be present in the ledger's VALIDATING blocks, so the same
`(root, description)` pair may appear twice in `pending`. That is harmless: `check_duplicate`
short-circuits on the first match and never counts.

In `_accept`, replace the single-line record with the bounded insert:

```python
        if len(self._accepted_in_flight) >= self.IN_FLIGHT_MEMORY_LIMIT:
            self._accepted_in_flight.pop(next(iter(self._accepted_in_flight)))
        self._accepted_in_flight[event.subproblem_id] = (
            event.meta.root_problem_id,
            event.description,
        )
```

`dict` preserves insertion order, so `next(iter(...))` is the oldest entry. Keep this before the
`await self.bus.publish(...)` call — the inline bus can re-enter this gate before publish returns.

The `Set` import added in Task 3 is now unused; remove it from the `typing` import line.

- [ ] **Step 5: Fix the stale docstring**

In `agenten/constitution/validators.py`, `check_duplicate`'s docstring says `pending_descriptions`
is "gathered by the caller from the ledger's VALIDATING-stage blocks". Since Task 3 that is
incomplete. Replace that sentence with:

```
    `pending_descriptions` is a list of (root_problem_id, description) pairs
    gathered by the caller from the ledger's VALIDATING-stage blocks plus any
    verdicts the gate has issued that the ledger has not persisted yet — kept
    as a plain argument here so this function stays a pure, dependency-free
    string comparison that's trivial to unit test. Duplicate pairs are
    harmless; the first match wins.
```

- [ ] **Step 6: Run the focused tests**

```bash
python -m pytest -q -o addopts="" tests/agenten/constitution/test_gatekeeper.py -v
```

Expected: all pass, including Task 3's three tests — the same-batch rejection, the memory release,
and root-scoping must all still hold. If `test_in_flight_memory_is_released_once_the_ledger_sees_the_subproblem`
now fails, that is expected and correct: pruning is no longer ledger-driven. Rewrite that test to
assert the eviction behaviour instead, and say so in your report rather than deleting it.

- [ ] **Step 7: Full suite and demo**

```bash
python -m pytest -q --no-cov -rs -m "not live" --ignore=tests/live
python main.py demo --output .captain-cook/task8-demo.json
```

Expected: suite green; `Demo complete: 4 subproblems reached done`.

- [ ] **Step 8: Commit and open the PR**

```bash
git add agenten/constitution/gatekeeper.py agenten/constitution/validators.py tests/agenten/constitution/test_gatekeeper.py
git commit -m "fix(constitution): bound the in-flight duplicate memory and drop the ledger sweep

The prune scan walked all ten stages on every proposal during a batch, and
terminal buckets never shrink, so it grew with total history on the hot path.
It also could not prune an acceptance the Recorder never persisted, which
turned a remembered verdict into a permanent ban. A bounded insertion-ordered
map covers the same window with no ledger access."
git push -u origin fix/gatekeeper-bounded-in-flight-memory
gh pr create --repo Flissel/Captain_cook --base main \
  --title "fix(constitution): bound the in-flight duplicate memory and drop the ledger sweep" \
  --body "Follow-up to #26. Removes the all-stage prune scan from the proposal hot path and caps the in-flight memory so an unpersisted acceptance cannot become a permanent duplicate ban."
```

---

## Execution order and independence

Tasks 1 → 2 → 4 are a chain: CI must work before the draft PR can go green, and Task 4's files only exist on `main` after that PR merges. Tasks 3, 5 and 6 are independent of the chain and of each other; any of them can be done first or in parallel.

If time is short, Task 1 alone is worth more than the rest combined — without it, nothing in this repository is automatically verified.

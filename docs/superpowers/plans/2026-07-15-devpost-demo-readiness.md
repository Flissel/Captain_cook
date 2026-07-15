# Devpost Demo Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an offline, reproducible Captain Cook demo and judge-facing OpenAI Build Week materials.

**Architecture:** `agenten/demo.py` adapts the existing `SupplyChainPipeline` with `InMemoryStorage` and deterministic decomposition. It writes a JSON evidence artifact. `main.py` dispatches to this demo without importing legacy code. Documentation distinguishes this vertical slice from the planned Hermes/Codex fleet.

**Tech Stack:** Python 3.11, asyncio, argparse, JSON, pytest, existing AutoGen 0.7.5 dependencies.

## Global Constraints

- No API key, network, Docker daemon, or browser is needed for the default demo.
- Do not add a gateway, MariaDB, Hermes control, n8n, or Minibook coupling in this slice.
- Preserve `agenten/orchestration/pipeline.py` as the sole domain orchestrator.
- Public claims must distinguish working functionality from roadmap items.
- Use red → green → refactor for every behavior change; run `python -m pytest -q` before each commit.

---

### Task 1: Offline demo adapter and artifact contract

**Files:** Create `agenten/demo.py` and `tests/test_demo.py`.

**Interfaces:** `async def run_demo(output_path: Path | None = None) -> DemoSummary`; `DemoSummary` exposes `success`, `problem_id`, `terminal_count`, `done_count`, `blocks`, and `to_dict()`.

- [ ] Write a failing async test that imports `run_demo`, calls it with `tmp_path / "evidence/demo-run.json"`, and asserts: `summary.success is True`, `summary.done_count == 2`, and the output exists.
- [ ] Run `python -m pytest tests/test_demo.py::test_run_demo_writes_an_inspectable_success_artifact -v`; expect collection failure because `agenten.demo` does not exist.
- [ ] Implement two atomic deterministic `echo` candidates; build the existing pipeline with `Blockchain(storage=InMemoryStorage())`; submit one fixed problem; wait for two terminal subproblems; map only JSON-safe fields (`index`, `block_type`, `status`, `description`, `result`) to the summary; create output parents and write `summary.to_dict()`. Raise `RuntimeError` on non-convergence.
- [ ] Add a test asserting both subproblem blocks are `done`; run `python -m pytest tests/test_demo.py -v`; expect PASS.
- [ ] Commit with `git add agenten/demo.py tests/test_demo.py` and `git commit -m "feat: add offline pipeline demo artifact"`.

### Task 2: Stable command-line entry point

**Files:** Modify `main.py`; create `tests/test_main_cli.py`.

**Interfaces:** `python main.py demo --output PATH` exits 0 and writes valid JSON. The original LLM prototype remains only at `python main.py legacy`.

- [ ] Write a failing subprocess test invoking `[sys.executable, "main.py", "demo", "--output", str(output)]`; assert zero exit code and `json.loads(output.read_text())["success"] is True`.
- [ ] Run `python -m pytest tests/test_main_cli.py::test_demo_command_writes_successful_evidence -v`; expect failure because the current script has no dispatcher and imports legacy dependencies eagerly.
- [ ] Implement `argparse` commands `demo` and `legacy`; use `Path("artifacts/demo-run.json")` as demo default. The demo branch calls `asyncio.run(run_demo(args.output))`, prints its done count, and returns 0. Move legacy imports into `run_legacy()`.
- [ ] Run `python -m pytest tests/test_main_cli.py -v` and `python main.py demo --output artifacts/demo-run.json`; expect PASS and artifact.
- [ ] Commit with `git add main.py tests/test_main_cli.py artifacts/demo-run.json` and `git commit -m "feat: expose offline demo command"`.

### Task 3: Judge-facing repository package

**Files:** Modify `README.md`; create `.env.example`, `docs/DEMO.md`, `docs/VIDEO_SCRIPT.md`, `docs/DEVPOST_CHECKLIST.md`, `docs/THIRD_PARTY_NOTICES.md`, and `tests/test_submission_docs.py`.

**Interfaces:** Documentation consumes the demo command and `artifacts/demo-run.json`; it produces clone-to-demo instructions, a transparent capability matrix, video run-of-show, and submission checklist.

- [ ] Write a failing documentation test asserting that README contains `python main.py demo` and `artifacts/demo-run.json`, and that the video script and checklist exist.
- [ ] Run `python -m pytest tests/test_submission_docs.py -v`; expect failure because the README is a placeholder and documents are absent.
- [ ] Write README sections in this order: product claim; overview; ASCII architecture; quickstart; demo; artifact inspection; tests; working-now vs roadmap; Codex/GPT-5.6 provenance; platform; licensing; layout. Explain the artifact in `DEMO.md`; write a <3-minute run-of-show in `VIDEO_SCRIPT.md`; list remaining human actions in `DEVPOST_CHECKLIST.md`; state Minibook AGPL-3.0 and Hermes MIT in notices. Do not create a root license unless the user confirms it.
- [ ] Run `python -m pytest tests/test_submission_docs.py -v` and `rg -n "TODO|TBD" README.md docs/DEMO.md docs/VIDEO_SCRIPT.md docs/DEVPOST_CHECKLIST.md`; expect PASS and no vague content markers.
- [ ] Commit with `git add README.md .env.example docs tests/test_submission_docs.py` and `git commit -m "docs: prepare judge-facing project guide"`.

### Task 4: Evidence verifier and release gate

**Files:** Create `scripts/verify_submission.py` and `tests/test_verify_submission.py`.

**Interfaces:** `validate_submission(root: Path) -> list[str]`; running `python scripts/verify_submission.py` exits 0 only when docs and evidence are complete.

- [ ] Write a failing test: `assert validate_submission(Path(".")) == []`.
- [ ] Run `python -m pytest tests/test_verify_submission.py -v`; expect failure because `scripts.verify_submission` does not exist.
- [ ] Implement validation for README, artifact, demo guide, video script, checklist, and third-party notice. Parse artifact JSON and require `success`, `problem_id`, `done_count`, and `blocks`; list each missing or invalid condition. The executable prints errors to stderr, or `Submission evidence check passed.`.
- [ ] Run `python scripts/verify_submission.py`, `python main.py demo --output artifacts/demo-run.json`, and `python -m pytest -q`; all must exit 0.
- [ ] Commit with `git add scripts/verify_submission.py tests/test_verify_submission.py artifacts/demo-run.json` and `git commit -m "test: add submission evidence gate"`.

### Task 5: Final review audit

**Files:** Modify `docs/DEVPOST_CHECKLIST.md`.

- [ ] Run `git status --short --branch`, `git submodule status`, `python scripts/verify_submission.py`, `python main.py demo --output artifacts/demo-run.json`, and `python -m pytest -q`.
- [ ] Record observed commit, test count, artifact path, and only the external human actions still needed: repository publication, Devpost form, public YouTube upload, and primary `/feedback` session ID.
- [ ] Commit with `git add docs/DEVPOST_CHECKLIST.md artifacts/demo-run.json` and `git commit -m "docs: record devpost readiness audit"`.

# AgentFarm Input Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Captain-owned, bounded AutoGen Society-of-Mind evaluator that turns the immutable AgentFarm `input.md` into QA-approved component subtask plans with planned acceptance tests and an inspectable Markdown report.

**Architecture:** Deterministic source ingestion creates immutable, redacted Markdown blocks. A `SocietyOfMindAgent` wraps a bounded `RoundRobinGroupChat` of Source Analyst, Component Planner, and independent QA Reviewer; those agents can only call Captain-owned typed tools. A filesystem artifact store is the initial durable boundary and produces the Markdown evidence report; its models map directly to a later MariaDB adapter.

**Tech Stack:** Python 3.11, Pydantic 2.13, AutoGen Core/AgentChat/Extensions 0.7.5, OpenAI `ChatCompletionClient`, pytest, standard-library `graphlib`, JSON and Markdown artifacts.

## Global Constraints

- Keep Captain as the sole validator and authoritative artifact writer; neither AutoGen, Hermes, Minibook, nor a transcript may release a work batch.
- Use only AutoGen 0.7.5 APIs; do not install or import `pyautogen` or the legacy AutoGen 0.2 API.
- Verify the source file's SHA-256 before a live LLM call; treat the local absolute path as configuration and persist only the safe logical source reference.
- Use `SocietyOfMindAgent` only as a presentation wrapper; persist every boundary in Captain because the inner team resets after a Society response.
- Configure the OpenAI client with `parallel_tool_calls=False`; allow at most three Planner/QA rounds per component and ten AgentChat turns per slice.
- The evaluator is planning-only: no Codex, n8n, browser, CRM, Mailpit, Minibook, Hermes, or workspace mutation capability.
- Store generated artifacts in gitignored `artifacts/evaluations/`; never record credentials, raw API responses, hidden holdouts, or unredacted sensitive source text.
- A live test must be marked `pytest.mark.live`, require `OPENAI_API_KEY` and `AGENTFARM_INPUT_PATH`, and report a missing prerequisite as a skipped live gate, not as passing evidence.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agenten/evaluation/models.py` | Frozen, versioned Pydantic contracts for sources, runs, inventory, plans, QA reviews, receipts, and statuses. |
| `agenten/evaluation/source.py` | Deterministic source verification, Markdown-section block construction, and redaction before model access. |
| `agenten/evaluation/validation.py` | Deterministic inventory, citation, test-plan, and dependency-DAG validation. |
| `agenten/evaluation/store.py` | Atomic append-only JSON artifact store and idempotency/conflict behavior. |
| `agenten/evaluation/report.py` | Deterministic `evaluation.md` renderer from stored artifacts only. |
| `agenten/evaluation/tools.py` | Captain-owned typed tool adapters exposed to AgentChat. |
| `agenten/evaluation/society.py` | AutoGen 0.7 Society-of-Mind and Round-Robin adapter, injected with model client and tools. |
| `agenten/evaluation/service.py` | Bounded evaluator coordinator that schedules slices, validates results, and finalizes runs. |
| `agenten/evaluation/cli.py` | Planning-only CLI that configures source path, model, limits, and output directory. |
| `tests/evaluation/*.py` | Deterministic unit, store, tool, report, and replay-style coordinator tests. |
| `tests/live/test_agentfarm_input_evaluation_live.py` | Explicit real-LLM smoke evaluation with a low component and call budget. |
| `.gitignore` | Ignores generated evaluation artifacts. |

## Task 1: Immutable Source and Evaluation Contracts

**Files:**
- Create: `agenten/evaluation/__init__.py`
- Create: `agenten/evaluation/models.py`
- Create: `agenten/evaluation/source.py`
- Test: `tests/evaluation/test_source.py`

**Interfaces:**
- Consumes: `agenten.planning.input_parser.ParsedProjectInput` and `MarkdownProjectInputParser`.
- Produces: `EvaluationSource`, `SourceBlock`, `EvaluationRun`, `EvaluationStatus`, `EvaluationSourceError`, and `load_evaluation_source(path, source_reference, max_block_bytes)`.

- [ ] **Step 1: Write the failing source-contract tests**

```python
def test_load_evaluation_source_preserves_digest_and_stable_blocks(tmp_path: Path) -> None:
    path = tmp_path / "input.md"
    path.write_text("# Team\n\nIntro\n\n## CRM\n\nBuild sync.\n", encoding="utf-8")

    source = load_evaluation_source(path, source_reference="agentfarm/input.md", max_block_bytes=64)

    assert source.source_reference == "agentfarm/input.md"
    assert source.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert [block.heading_path for block in source.blocks] == [("Team",), ("Team", "CRM")]
    assert all(block.sha256 == hashlib.sha256(block.text.encode("utf-8")).hexdigest() for block in source.blocks)


def test_load_evaluation_source_redacts_secret_like_content_before_model_access(tmp_path: Path) -> None:
    path = tmp_path / "input.md"
    path.write_text("# Team\n\nOPENAI_API_KEY=sk-test-secret\n", encoding="utf-8")

    source = load_evaluation_source(path, source_reference="agentfarm/input.md")

    assert "sk-test-secret" not in source.blocks[0].text
    assert "[REDACTED]" in source.blocks[0].text
```

- [ ] **Step 2: Run the source tests to verify failure**

Run: `python -m pytest -q tests/evaluation/test_source.py`

Expected: FAIL because `agenten.evaluation` does not exist.

- [ ] **Step 3: Implement frozen source models and deterministic segmentation**

```python
class SourceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    block_id: str = Field(pattern=r"^block-[0-9]{4}$")
    heading_path: tuple[str, ...] = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)


def load_evaluation_source(path: Path, *, source_reference: str, max_block_bytes: int = 12_000) -> EvaluationSource:
    parsed = MarkdownProjectInputParser().parse(path, source_reference=source_reference)
    blocks = tuple(_block_sections(parsed, max_block_bytes=max_block_bytes))
    _reject_secret_like_content(blocks)
    return EvaluationSource(
        source_reference=parsed.source_reference,
        sha256=parsed.sha256,
        byte_length=parsed.byte_length,
        blocks=blocks,
    )
```

In the same module define `EvaluationStatus` with `created`, `inventorying`, `planning`, `accepted`, `partial`, and `failed`, plus a frozen `EvaluationRun(run_id, idempotency_key, source, status, max_rounds, max_calls)`. Use `ParsedProjectInput.sections` rather than reparsing Markdown. Split an oversized section only at line boundaries, retain the same heading path, and derive each stable block ID from ordered block position. Replace values in high-confidence credential assignments (`*_API_KEY=`, `*_TOKEN=`, `password=`) with `[REDACTED]` before a block can reach an LLM or artifact store; reject only content whose sensitive portion cannot be deterministically removed.

- [ ] **Step 4: Run focused source tests and the existing parser tests**

Run: `python -m pytest -q tests/evaluation/test_source.py tests/planning/test_input_parser.py`

Expected: PASS with no skipped tests.

- [ ] **Step 5: Commit the isolated source contract**

```powershell
git add agenten/evaluation/__init__.py agenten/evaluation/models.py agenten/evaluation/source.py tests/evaluation/test_source.py
git commit -m "feat: add agentfarm evaluation source contract"
```

## Task 2: Component, Test-Plan, and QA Validation Contracts

**Files:**
- Modify: `agenten/evaluation/models.py`
- Create: `agenten/evaluation/validation.py`
- Test: `tests/evaluation/test_validation.py`

**Interfaces:**
- Consumes: `EvaluationSource` and candidate/review records from `models.py`.
- Produces: `ComponentInventoryCandidate`, `ComponentPlanCandidate`, `AcceptanceTestPlan`, `QaReview`, `ValidationIssue`, `validate_inventory`, `validate_candidate`, and `validate_component_graph`.

- [ ] **Step 1: Write failing schema and DAG tests**

```python
def test_candidate_requires_a_complete_observable_acceptance_test() -> None:
    with pytest.raises(ValidationError, match="expected"):
        AcceptanceTestPlan(test_id="crm-unit", test_type="unit", setup="fixture", action="call sync", expected="", command="pytest")


def test_component_graph_rejects_unknown_dependency_and_cycle() -> None:
    plans = (candidate("crm", dependencies=("email",)), candidate("email", dependencies=("crm",)))

    issues = validate_component_graph(plans)

    assert {issue.code for issue in issues} == {"dependency_cycle"}
```

- [ ] **Step 2: Run validation tests to verify failure**

Run: `python -m pytest -q tests/evaluation/test_validation.py`

Expected: FAIL because candidate and validator contracts are missing.

- [ ] **Step 3: Implement strict candidate and QA models**

```python
class AcceptanceTestPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    test_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    test_type: Literal["unit", "integration", "contract", "live"]
    setup: str = Field(min_length=1)
    action: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    command: str = Field(min_length=1)


class QaReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    component_key: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    revision: int = Field(ge=1, le=3)
    decision: Literal["approved", "revision_required"]
    rubric_score: int = Field(ge=0, le=7)
    defect_codes: tuple[RubricCode, ...] = ()
    revision_requests: tuple[str, ...] = ()
```

Use a fixed `RubricCode` literal set: `missing_citation`, `duplicate_scope`, `unknown_dependency`, `dependency_cycle`, `incomplete_implementation`, `missing_test`, `weak_test_oracle`, `wrong_test_level`, and `false_execution_claim`. Reject `approved` reviews that have defect codes or a score below seven. Use `graphlib.TopologicalSorter` to distinguish cycles from unknown dependencies.

- [ ] **Step 4: Run focused validation and planning-policy regression tests**

Run: `python -m pytest -q tests/evaluation/test_validation.py tests/planning/test_policy.py tests/planning/test_policy_integration.py`

Expected: PASS with no mutation of existing planning contracts.

- [ ] **Step 5: Commit validation contracts**

```powershell
git add agenten/evaluation/models.py agenten/evaluation/validation.py tests/evaluation/test_validation.py
git commit -m "feat: validate evaluation component plans"
```

## Task 3: Append-Only Artifact Store and Markdown Evidence

**Files:**
- Modify: `agenten/evaluation/models.py`
- Create: `agenten/evaluation/store.py`
- Create: `agenten/evaluation/report.py`
- Modify: `.gitignore`
- Test: `tests/evaluation/test_store.py`
- Test: `tests/evaluation/test_report.py`

**Interfaces:**
- Consumes: source, inventory, candidates, and reviews from Tasks 1-2.
- Produces: `EvaluationOutcome`, `EvaluationManifest`, `ComponentOutcome`, `InventoryReceipt`, `CandidateReceipt`, `ReviewReceipt`, `EvaluationConflictError`, `JsonEvaluationStore.create_run`, `stage_inventory`, `stage_candidate`, `record_review`, `finalize`, and `render_evaluation_markdown`.

- [ ] **Step 1: Write failing append-only, idempotency, and redaction tests**

```python
@pytest.mark.asyncio
async def test_store_replays_identical_candidate_and_rejects_changed_revision(tmp_path: Path) -> None:
    store = JsonEvaluationStore(tmp_path)
    run = await store.create_run(source(), run_id="eval-001", idempotency_key="input-v1")

    first = await store.stage_candidate(run.run_id, candidate("crm", revision=1))
    replay = await store.stage_candidate(run.run_id, candidate("crm", revision=1))

    assert first == replay
    with pytest.raises(EvaluationConflictError, match="already staged differently"):
        await store.stage_candidate(run.run_id, candidate("crm", revision=1, scope="changed"))


def test_report_contains_planned_not_executed_and_redacts_secret(tmp_path: Path) -> None:
    report = render_evaluation_markdown(manifest_with_text("OPENAI_API_KEY=secret"))
    assert "planned, not executed" in report
    assert "secret" not in report
```

- [ ] **Step 2: Run store and report tests to verify failure**

Run: `python -m pytest -q tests/evaluation/test_store.py tests/evaluation/test_report.py`

Expected: FAIL because the artifact store and renderer are missing.

- [ ] **Step 3: Implement atomic JSON persistence and report rendering**

```python
class JsonEvaluationStore:
    async def create_run(self, source: EvaluationSource, *, run_id: str, idempotency_key: str) -> EvaluationRun: ...
    async def stage_inventory(self, run_id: str, inventory: ComponentInventoryCandidate) -> InventoryReceipt: ...
    async def stage_candidate(self, run_id: str, candidate: ComponentPlanCandidate) -> CandidateReceipt: ...
    async def record_review(self, run_id: str, review: QaReview) -> ReviewReceipt: ...
    async def finalize(self, run_id: str, outcome: EvaluationOutcome) -> EvaluationManifest: ...
```

Follow `JsonDirectoryReleaseClient` semantics: use one `asyncio.Lock`, compare existing JSON before writes, write a sibling `.tmp` file, then atomically replace it. Persist source manifest first, then component inventory, candidate revisions, QA reviews, manifest, and finally `evaluation.md`. Add `artifacts/evaluations/` to `.gitignore`.

Add the listed receipt, outcome, manifest, component-outcome, and conflict types to `models.py` in this task. `EvaluationManifest` must include `status: EvaluationStatus`, source provenance, component outcomes, model identifier, prompt version, call count, token totals, cost total, artifact digests, and the fixed planning-only disclaimer.

The Markdown renderer must use only the stored manifest and must list source digest, model and prompt versions, time/cost/call totals, every accepted plan, QA results, unresolved components, and the literal statement: `Acceptance tests are planned, not executed by this evaluation.`

- [ ] **Step 4: Run focused artifact tests and release-adapter regression tests**

Run: `python -m pytest -q tests/evaluation/test_store.py tests/evaluation/test_report.py tests/planning/test_release.py`

Expected: PASS and repeated identical writes leave byte-identical artifacts.

- [ ] **Step 5: Commit artifact storage and evidence report**

```powershell
git add .gitignore agenten/evaluation/store.py agenten/evaluation/report.py tests/evaluation/test_store.py tests/evaluation/test_report.py
git commit -m "feat: persist agentfarm evaluation evidence"
```

## Task 4: Captain-Owned Evaluation Tool Boundary

**Files:**
- Create: `agenten/evaluation/tools.py`
- Test: `tests/evaluation/test_tools.py`

**Interfaces:**
- Consumes: `JsonEvaluationStore`, `EvaluationSource`, and deterministic validators.
- Produces: `EvaluationToolService.read_source_block`, `stage_component_inventory`, `stage_component_plan`, and `record_qa_review`; each returns a typed receipt or raises `EvaluationToolError`.

- [ ] **Step 1: Write failing authority-boundary tests**

```python
@pytest.mark.asyncio
async def test_tool_service_returns_only_redacted_source_views(service: EvaluationToolService) -> None:
    view = await service.read_source_block("eval-001", "block-0001")
    assert view.block_id == "block-0001"
    assert "OPENAI_API_KEY" not in view.text


@pytest.mark.asyncio
async def test_tool_service_rejects_plan_before_inventory_and_review_of_missing_candidate(service: EvaluationToolService) -> None:
    with pytest.raises(EvaluationToolError, match="inventory"):
        await service.stage_component_plan("eval-001", candidate("crm"))
    with pytest.raises(EvaluationToolError, match="candidate"):
        await service.record_qa_review("eval-001", approved_review("crm", revision=1))
```

- [ ] **Step 2: Run tool tests to verify failure**

Run: `python -m pytest -q tests/evaluation/test_tools.py`

Expected: FAIL because `EvaluationToolService` is absent.

- [ ] **Step 3: Implement typed tool adapters with no release capability**

```python
class EvaluationToolService:
    async def read_source_block(self, run_id: str, block_id: str) -> SourceBlockView: ...
    async def stage_component_inventory(self, run_id: str, inventory: ComponentInventoryCandidate) -> InventoryReceipt: ...
    async def stage_component_plan(self, run_id: str, candidate: ComponentPlanCandidate) -> CandidateReceipt: ...
    async def record_qa_review(self, run_id: str, review: QaReview) -> ReviewReceipt: ...
```

Validate `run_id`, source citations, component inventory membership, revision monotonicity, QA rubric codes, and the three-round ceiling before calling the store. Do not expose `finalize`, work-batch release, Codex, n8n, filesystem paths, environment variables, or arbitrary tool names to AgentChat.

- [ ] **Step 4: Run focused tool, store, and capability-boundary tests**

Run: `python -m pytest -q tests/evaluation/test_tools.py tests/evaluation/test_store.py tests/agent_runtime/test_capabilities.py`

Expected: PASS with no new capability grant path.

- [ ] **Step 5: Commit the tool boundary**

```powershell
git add agenten/evaluation/tools.py tests/evaluation/test_tools.py
git commit -m "feat: add captain evaluation tool boundary"
```

## Task 5: Bounded AutoGen Society-of-Mind Coordinator

**Files:**
- Create: `agenten/evaluation/society.py`
- Create: `agenten/evaluation/service.py`
- Test: `tests/evaluation/test_society.py`
- Test: `tests/evaluation/test_service.py`

**Interfaces:**
- Consumes: injected `ChatCompletionClient`, `EvaluationToolService`, `JsonEvaluationStore`, and `EvaluationRun` limits.
- Produces: `build_evaluation_society`, `AgentFarmEvaluationService.run`, and durable `accepted`, `partial`, `unresolved`, or `failed` outcomes.

- [ ] **Step 1: Write failing bounded-loop and non-authority tests**

```python
@pytest.mark.asyncio
async def test_service_accepts_only_qa_approved_candidates_and_writes_report(tmp_path: Path) -> None:
    service = scripted_service(tmp_path, inventory=("crm",), qa_decisions=("revision_required", "approved"))

    manifest = await service.run(run_id="eval-001")

    assert manifest.status is EvaluationStatus.ACCEPTED
    assert manifest.component_outcomes["crm"].rounds == 2
    assert (tmp_path / "eval-001" / "evaluation.md").is_file()


@pytest.mark.asyncio
async def test_service_marks_third_rejection_unresolved_without_a_fourth_planner_call(tmp_path: Path) -> None:
    service = scripted_service(tmp_path, inventory=("crm",), qa_decisions=("revision_required",) * 3)

    manifest = await service.run(run_id="eval-001")

    assert manifest.status is EvaluationStatus.PARTIAL
    assert manifest.component_outcomes["crm"].status == "unresolved"
    assert service.planner_calls == 3
```

- [ ] **Step 2: Run coordinator tests to verify failure**

Run: `python -m pytest -q tests/evaluation/test_society.py tests/evaluation/test_service.py`

Expected: FAIL because the Society adapter and service are missing.

- [ ] **Step 3: Implement the AutoGen adapter and scheduler**

```python
def build_evaluation_society(
    *,
    model_client: ChatCompletionClient,
    tools: EvaluationToolService,
    max_rounds: int = 3,
) -> SocietyOfMindAgent:
    analyst = AssistantAgent("source_analyst", model_client=model_client, tools=[tools.read_source_block, tools.stage_component_inventory], system_message=ANALYST_PROMPT)
    planner = AssistantAgent("component_planner", model_client=model_client, tools=[tools.stage_component_plan], system_message=PLANNER_PROMPT)
    reviewer = AssistantAgent("qa_reviewer", model_client=model_client, tools=[tools.record_qa_review], system_message=QA_PROMPT)
    inner = RoundRobinGroupChat([analyst, planner, reviewer], max_turns=10, termination_condition=TextMentionTermination("EVALUATION_SLICE_COMPLETE"))
    return SocietyOfMindAgent("agentfarm_evaluator", team=inner, model_client=model_client, instruction=SUMMARY_PROMPT, response_prompt=SUMMARY_RESPONSE_PROMPT)
```

Set `parallel_tool_calls=False` in the OpenAI model-client construction used by the CLI. Keep `build_evaluation_society` dependency-injected for deterministic tests. `AgentFarmEvaluationService` invokes one Society slice for inventory, then one bounded slice per inventory component; it reads the persisted receipts after every slice and never parses the Society's prose as authority. It finalizes through `JsonEvaluationStore` only after `validate_component_graph` and approved-review checks pass.

- [ ] **Step 4: Run deterministic coordinator, replay, and architecture tests**

Run: `python -m pytest -q tests/evaluation/test_society.py tests/evaluation/test_service.py tests/test_architecture_fitness.py tests/test_import_boundaries.py`

Expected: PASS. Tests must prove no finalization path is available to an AutoGen tool and that a resumed run reads persisted state rather than an inner-team transcript.

- [ ] **Step 5: Commit the bounded Society coordinator**

```powershell
git add agenten/evaluation/society.py agenten/evaluation/service.py tests/evaluation/test_society.py tests/evaluation/test_service.py
git commit -m "feat: add bounded agentfarm evaluation society"
```

## Task 6: CLI, Explicit Live LLM Gate, and Documentation

**Files:**
- Create: `agenten/evaluation/cli.py`
- Modify: `agenten/evaluation/__init__.py`
- Modify: `.env.example`
- Create: `tests/evaluation/test_evaluation_cli.py`
- Create: `tests/live/test_agentfarm_input_evaluation_live.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `AGENTFARM_INPUT_PATH`, `OPENAI_API_KEY`, optional `CAPTAIN_MODEL`, plus prior tasks' service.
- Produces: `python -m agenten.evaluation.cli`, a safe JSON stdout summary, and one explicitly live `evaluation.md` artifact.

- [ ] **Step 1: Write failing CLI and live-gate prerequisite tests**

```python
@pytest.mark.asyncio
async def test_cli_requires_a_safe_logical_source_reference(tmp_path: Path) -> None:
    input_path = tmp_path / "input.md"
    input_path.write_text("# Team\n\nBuild CRM.\n", encoding="utf-8")

    assert await async_main([str(input_path), "--source-reference", "../unsafe"]) == 1


@pytest.mark.live
def test_live_agentfarm_evaluation_requires_explicit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFARM_INPUT_PATH", raising=False)
    with pytest.raises(pytest.skip.Exception, match="AGENTFARM_INPUT_PATH"):
        require_live_evaluation_environment()
```

- [ ] **Step 2: Run CLI tests to verify failure**

Run: `python -m pytest -q tests/evaluation/test_evaluation_cli.py`

Expected: FAIL because the evaluator CLI does not exist.

- [ ] **Step 3: Implement planning-only CLI and live smoke test**

```python
parser.add_argument("input", type=Path, help="immutable AgentFarm Markdown input")
parser.add_argument("--source-reference", required=True)
parser.add_argument("--output", type=Path, default=Path("artifacts/evaluations"))
parser.add_argument("--run-id")
parser.add_argument("--model")
parser.add_argument("--max-components", type=int, default=1)
parser.add_argument("--max-rounds", type=int, choices=(1, 2, 3), default=3)
parser.add_argument("--max-calls", type=int, default=8)
```

The live test must use `AGENTFARM_INPUT_PATH`, validate the expected source digest before starting, set `max_components=1`, `max_rounds=1`, and `max_calls=8`, then assert the manifest records a real model identifier and positive usage while stating that acceptance tests were not executed. Eight calls are the bounded real-AgentChat allowance for three tool turns plus required post-tool completions. It must create artifacts only under pytest's temporary directory. Do not print the API key, raw prompt, raw response, or source text.

Document one normal command and one cost-bounded live command in `README.md`; add only `AGENTFARM_INPUT_PATH=` and evaluation budget names to `.env.example`, never a source path or secret value.

- [ ] **Step 4: Run CLI, live-gate skip, and selected full tests**

Run: `python -m pytest -q tests/evaluation/test_evaluation_cli.py tests/evaluation/test_service.py tests/live/test_agentfarm_input_evaluation_live.py`

Expected: deterministic tests PASS; the live test is SKIPPED with a clear prerequisite message unless both `OPENAI_API_KEY` and `AGENTFARM_INPUT_PATH` are deliberately configured.

- [ ] **Step 5: Run the authorized real-LLM gate separately**

Run: `$env:AGENTFARM_INPUT_PATH='C:\\Users\\User\\Desktop\\Vibemind_V1\\vibemind-os\\voice\\external\\Autogen_AgentFarm\\input.md'; python -m pytest -q -m live tests/live/test_agentfarm_input_evaluation_live.py`

Expected: PASS only if the source digest matches, a real configured model answers within the eight-call budget, and the resulting report is clearly labeled as a plan evaluation. Report actual model ID, call count, token usage, artifact path, and any non-green status; never treat a skip or provider failure as green.

- [ ] **Step 6: Run final project gates and commit**

Run: `python -m pytest -q tests/evaluation tests/planning/test_input_parser.py tests/test_architecture_fitness.py tests/test_import_boundaries.py`

Run: `python -m compileall -q agenten`

Expected: PASS; report any separately skipped MariaDB, Minibook, or live-service gates.

```powershell
git add .env.example README.md agenten/evaluation/__init__.py agenten/evaluation/cli.py tests/evaluation/test_evaluation_cli.py tests/live/test_agentfarm_input_evaluation_live.py
git commit -m "feat: add agentfarm evaluation cli"
```

## Plan Self-Review

- Spec coverage: Tasks 1-2 implement immutable source, inventory, component, QA, test-plan, and dependency rules. Tasks 3-4 implement append-only evidence and Captain-only tools. Task 5 implements bounded AutoGen Society-of-Mind scheduling and recovery from stored state. Task 6 implements the planning CLI, explicit live LLM gate, documentation, and final verification.
- Placeholder scan: no task relies on an unnamed type, unbounded loop, unspecified test, or deferred validation behavior.
- Type consistency: `EvaluationSource` and `SourceBlock` originate in Task 1; candidates and reviews in Task 2; the store in Task 3; tool adapters in Task 4; AutoGen depends only on those typed adapters in Task 5; CLI depends on the completed service in Task 6.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-18-agentfarm-input-evaluation.md`. Two execution options:

1. Subagent-Driven (recommended) — dispatch a fresh subagent per task and review each task before the next.
2. Inline Execution — execute the tasks in this session with checkpoints for review.

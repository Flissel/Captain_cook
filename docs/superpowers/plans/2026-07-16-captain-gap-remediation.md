# Captain Gap Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Program routing:** Do not dispatch this plan standalone. Follow
> `2026-07-16-remediation-program-orchestration.md`. Captain Tasks 1, 3, 6, 7,
> and the shared-document part of Task 8 are absorbed by canonical program
> packets; only the orchestrator updates source-plan checkboxes.

**Goal:** Make the Captain planning path production-ready by enforcing deterministic policy after every LLM stage, releasing through the sole-writer gateway, resuming partial project releases, and proving live persistence and runtime contracts.

**Architecture:** Keep the deterministic `CaptainPipeline` independent of transports and databases. Add typed policy, release, capability, run-store, and event-publication ports; wire JSON implementations for offline operation and HTTP/MariaDB-backed implementations for production. Hermes and n8n remain external consumers and are not implemented here.

**Tech Stack:** Python 3.11, Pydantic 2.13.4, AutoGen Core/AgentChat 0.7.5, FastAPI 0.139.0, httpx 0.28.1, MariaDB 11.8, pytest.

## Global Constraints

- Work from the latest `main` in an isolated feature worktree; never develop from the stale `feat/householder-runtime` checkout.
- Preserve the deterministic offline path and its JSON artifacts.
- Captain owns planning, policy, gateway contracts, project status, and orchestration only; Hermes and n8n remain external systems.
- Holdouts must never appear in build-visible batches, logs, model repair prompts, or capability descriptors.
- Secrets stay in gitignored environment/config files and never enter fixtures, commits, logs, or artifacts.
- All behavioral changes follow RED → GREEN → REFACTOR and end in a narrow Conventional Commit.
- Do not claim MariaDB or gateway behavior from mocks; the live gate requires `TEST_MARIADB_DSN` and zero gateway/MariaDB skips.

---

## File Structure

- `agenten/planning/policy.py` — deterministic post-LLM vocabulary and case-isolation rules.
- `agenten/planning/gateway_client.py` — HTTP release and validated-capability adapters.
- `agenten/planning/run_models.py` — durable project-run state and typed partial-release errors.
- `agenten/planning/run_store.py` — JSON run-store port implementation for offline resume.
- `agenten/planning/captain_pipeline.py` — orchestration only; consumes the new ports.
- `agenten/planning/factory.py` — selects offline or gateway composition without importing infrastructure into domain modules.
- `agenten/llm/resilience.py` — bounded timeout/retry wrapper and typed LLM-stage failures.
- `agenten/runtime/event_bus.py` — publication-only port plus in-process subscription capability.
- `agenten/runtime/autogen_bus.py` — implements publication without a fake callable-subscription method.
- `gateway/app.py` — idempotent Captain pre-claim writes and capability-query contract.
- `tests/planning/`, `tests/runtime/`, `tests/gateway/` — acceptance and contract tests.

---

### Task 1: Establish the canonical integration baseline

**Files:**
- Modify: `docs/WORKSTREAMS.md`
- Modify: `docs/superpowers/plans/2026-07-15-architecture-gap-todos.md`
- Test: `tests/test_workstream_docs.py`

**Interfaces:**
- Consumes: local `main` containing Captain planning, gateway, delivery control plane, setup, and unroutable outcomes.
- Produces: one documented integration baseline and a machine-checkable assertion that active branches derive from it.

- [ ] **Step 1: Write the failing workstream assertion**

```python
def test_main_is_the_canonical_integration_baseline() -> None:
    text = Path("docs/WORKSTREAMS.md").read_text(encoding="utf-8")
    assert "`main` is the canonical integration baseline" in text
    assert "feat/devpost-demo-readiness` is the current reviewable baseline" not in text
```

- [ ] **Step 2: Verify the assertion fails**

Run: `python -m pytest -q tests/test_workstream_docs.py::test_main_is_the_canonical_integration_baseline`

Expected: FAIL because the document still names `feat/devpost-demo-readiness`.

- [ ] **Step 3: Record the integration rule and active dependency order**

Add this exact rule to `docs/WORKSTREAMS.md` and mark the matching P0 checkbox complete in the architecture backlog:

```markdown
`main` is the canonical integration baseline. New feature work starts from the
current local/remote `main`; stale worktrees are fast-forwarded or retired
before editing shared files.
```

- [ ] **Step 4: Simulate active dependency merges**

Run:

```powershell
git merge-base --is-ancestor feat/householder-runtime main
git merge-tree $(git merge-base main feat/release-evidence) main feat/release-evidence |
  Select-String '<<<<<<<|CONFLICT'
git merge-tree $(git merge-base main feat/worker-fleet) main feat/worker-fleet |
  Select-String '<<<<<<<|CONFLICT'
```

Expected: the runtime branch is already contained in `main`; merge-tree prints no conflicts. Record any non-conflicting overlap in the backlog rather than editing foreign branches.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest -q tests/test_workstream_docs.py tests/test_architecture_fitness.py`

Commit:

```powershell
git add docs/WORKSTREAMS.md docs/superpowers/plans/2026-07-15-architecture-gap-todos.md tests/test_workstream_docs.py
git commit -m "docs: establish canonical integration baseline"
```

---

### Task 2: Enforce deterministic post-enrichment policy

**Files:**
- Create: `agenten/planning/policy.py`
- Modify: `agenten/planning/captain_pipeline.py`
- Modify: `agenten/planning/factory.py`
- Test: `tests/planning/test_policy.py`
- Test: `tests/planning/test_captain_pipeline.py`

**Interfaces:**
- Consumes: configured `allowed_capability_tags: frozenset[str]`, `BatchEnrichment`, and `WorkBatch`.
- Produces: `PlanningPolicy.validate_enrichment(enrichment) -> None` and deterministic content fingerprints for golden/holdout isolation.

- [x] **Step 1: Write failing policy tests**

```python
def test_policy_rejects_enrichment_capability_outside_vocabulary() -> None:
    policy = PlanningPolicy(frozenset({"delivery"}))
    enrichment = enrichment_fixture(capability_tags=["invented"])
    with pytest.raises(PlanningPolicyError, match="unknown capability tags"):
        policy.validate_enrichment(enrichment)


def test_policy_rejects_same_case_content_under_different_ids() -> None:
    policy = PlanningPolicy(frozenset({"delivery"}))
    enrichment = enrichment_fixture(
        golden_cases=[ExampleCase(case_id="visible", input={"score": 82})],
        holdout_cases=[ExampleCase(case_id="hidden", input={"score": 82})],
    )
    with pytest.raises(PlanningPolicyError, match="holdout content overlaps"):
        policy.validate_enrichment(enrichment)
```

- [x] **Step 2: Verify both tests fail**

Run: `python -m pytest -q tests/planning/test_policy.py`

Expected: collection failure because `PlanningPolicy` does not exist.

- [x] **Step 3: Implement canonical fingerprints and validation**

```python
class PlanningPolicyError(ValueError):
    pass


class PlanningPolicy:
    def __init__(self, allowed_capability_tags: frozenset[str]) -> None:
        self.allowed_capability_tags = allowed_capability_tags

    @staticmethod
    def _fingerprint(case: ExampleCase) -> str:
        payload = case.model_dump(mode="json", exclude={"case_id"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def validate_enrichment(self, enrichment: BatchEnrichment) -> None:
        unknown = sorted(set(enrichment.capability_tags) - self.allowed_capability_tags)
        if unknown:
            raise PlanningPolicyError(f"unknown capability tags: {unknown}")
        visible = {self._fingerprint(case) for case in enrichment.golden_cases}
        hidden = {self._fingerprint(case) for case in enrichment.holdout_cases}
        if visible & hidden:
            raise PlanningPolicyError("holdout content overlaps build-visible golden content")
```

- [x] **Step 4: Inject policy into the pipeline and factory**

Add `policy: PlanningPolicy` to `CaptainPipeline.__init__`, call `self._policy.validate_enrichment(enrichment)` immediately after each enrichment, and construct it in `build_captain_pipeline` from `known_capability_tags`.

- [x] **Step 5: Verify and commit**

Run: `python -m pytest -q tests/planning/test_policy.py tests/planning/test_captain_pipeline.py tests/planning/test_factory_e2e.py`

Commit:

```powershell
git add agenten/planning/policy.py agenten/planning/captain_pipeline.py agenten/planning/factory.py tests/planning
git commit -m "feat: enforce captain planning policy"
```

---

### Task 3: Add gateway release and capability adapters

> **Program routing:** P07C owns Step 4's gateway-side idempotency after the
> append-only store is integrated. P11 later owns Steps 1-3 and 5-6 plus the
> planning HTTP adapter. Do not let either worker edit the other's allowlist.

**Files:**
- Create: `agenten/planning/gateway_client.py`
- Modify: `agenten/planning/factory.py`
- Modify: `agenten/planning/cli.py`
- Modify for P07C only: `gateway/store.py`
- Test: `tests/planning/test_gateway_client.py`
- Test: `tests/gateway/test_gateway.py`

**Interfaces:**
- Consumes: `BatchReleaseClient`, `CapabilityResolver`, gateway `POST /blocks`, `GET /batches/{id}/blocks`, and `GET /capabilities?need=`.
- Produces: `GatewayPlanningClient(base_url, client)` implementing both planning ports with idempotent replay.

- [ ] **Step 1: Write failing HTTP contract tests with an ASGI transport**

```python
@pytest.mark.asyncio
async def test_gateway_client_releases_batch_then_hidden_suite() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        index = 41 if len(requests) == 1 else 42
        return httpx.Response(201, json={"index": index}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", http)
        await client.release(batch_fixture(), holdout_fixture())

    payloads = [json.loads(request.content) for request in requests]
    assert [payload["block_type"] for payload in payloads] == ["work_batch", "holdout"]
    assert payloads[1]["parent_index"] == 41


@pytest.mark.asyncio
async def test_gateway_client_treats_identical_existing_batch_as_success() -> None:
    responses = iter([
        (409, {"detail": "batch_id already exists"}),
        (200, [{"block_type": "work_batch", "data": batch_fixture().model_dump(mode="json")},
               {"block_type": "holdout", "data": holdout_fixture().model_dump(mode="json")}]),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        status, payload = next(responses)
        return httpx.Response(status, json=payload, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayPlanningClient("http://gateway", http)
        await client.release(batch_fixture(), holdout_fixture())
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest -q tests/planning/test_gateway_client.py`

Expected: collection failure because `GatewayPlanningClient` does not exist.

- [ ] **Step 3: Implement the adapter**

```python
class GatewayPlanningClient(BatchReleaseClient, CapabilityResolver):
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def release(self, batch: WorkBatch, holdouts: HoldoutSuite) -> None:
        batch_response = await self._client.post(
            f"{self._base_url}/blocks",
            json={"block_type": "work_batch", "data": batch.model_dump(mode="json")},
        )
        if batch_response.status_code == 409:
            await self._assert_existing_release_matches(batch, holdouts)
            return
        batch_response.raise_for_status()
        parent_index = int(batch_response.json()["index"])
        hidden_response = await self._client.post(
            f"{self._base_url}/blocks",
            json={
                "block_type": "holdout",
                "parent_index": parent_index,
                "data": holdouts.model_dump(mode="json"),
            },
        )
        hidden_response.raise_for_status()

    async def find_match(self, target: str, capability_tags: list[str]) -> str | None:
        need = " ".join([target, *sorted(capability_tags)])
        response = await self._client.get(f"{self._base_url}/capabilities", params={"need": need})
        response.raise_for_status()
        matches = response.json()
        return str(matches[0]["artifact_ref"]) if matches else None
```

- [ ] **Step 4: Make Captain writes idempotent at the gateway**

Before returning `409` for an existing `work_batch`, compare the stored canonical data to the validated request. Return the existing block when identical; retain `409` for different content. Apply the same rule to one `holdout` child per batch.

P07C verifies this rule through the isolated MariaDB gate and commits only its
store boundary:

```powershell
pwsh -NoProfile -File scripts/test_gateway.ps1
git add gateway/store.py tests/gateway/test_gateway.py
git commit -m "feat: make gateway releases idempotent"
```

- [ ] **Step 5: Add explicit composition flags**

Extend the CLI with `--release-mode {json,gateway}` and `--gateway-url`. `json` remains the default. `gateway` constructs one `httpx.AsyncClient`, uses `GatewayPlanningClient` as both release client and capability resolver, and closes the client after the run.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest -q --no-cov tests/planning/test_gateway_client.py tests/planning/test_factory_e2e.py tests/planning/test_cli.py`

Commit:

```powershell
git add agenten/planning/gateway_client.py agenten/planning/factory.py agenten/planning/cli.py tests/planning/test_gateway_client.py tests/planning/test_factory_e2e.py tests/planning/test_cli.py
git commit -m "feat: connect captain planning to ledger gateway"
```

---

### Task 4: Persist and resume partial Captain runs

**Files:**
- Create: `agenten/planning/run_models.py`
- Create: `agenten/planning/run_store.py`
- Modify: `agenten/planning/captain_pipeline.py`
- Modify: `agenten/planning/cli.py`
- Test: `tests/planning/test_run_store.py`
- Test: `tests/planning/test_resume.py`

**Interfaces:**
- Produces: `CaptainRunState`, `CaptainRunStatus`, `CaptainRunStore`, `JsonCaptainRunStore`, and `PartialReleaseError`.
- Invariant: a retry with the same `run_id` never enriches or releases a completed batch again.

- [ ] **Step 1: Write a failing crash/resume acceptance test**

```python
@pytest.mark.asyncio
async def test_run_resumes_at_first_unreleased_batch(tmp_path: Path) -> None:
    release = FailOnceOnBatch("second")
    store = JsonCaptainRunStore(tmp_path / "runs")
    pipeline = pipeline_fixture(release_client=release, run_store=store)
    with pytest.raises(PartialReleaseError) as failure:
        await pipeline.run("project", run_id="run-1")
    assert failure.value.released_batch_ids == ["first"]

    resumed = await pipeline.run("project", run_id="run-1")
    assert resumed.status is CaptainRunStatus.RELEASED
    assert release.calls == ["first", "second", "second"]
```

- [ ] **Step 2: Verify it fails**

Run: `python -m pytest -q tests/planning/test_resume.py`

Expected: `CaptainPipeline.run()` has no `run_id` or run-store support.

- [ ] **Step 3: Define durable state**

```python
class CaptainRunStatus(str, Enum):
    PLANNING = "planning"
    RELEASING = "releasing"
    PARTIALLY_RELEASED = "partially_released"
    RELEASED = "released"
    FAILED = "failed"


class CaptainRunState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    project_id: str
    project_digest: str
    status: CaptainRunStatus
    batches: list[WorkBatch]
    released_batch_ids: list[str] = Field(default_factory=list)
    failed_batch_id: str | None = None
    error_kind: str | None = None
```

- [ ] **Step 4: Implement atomic JSON run persistence**

`JsonCaptainRunStore.save()` writes canonical JSON to `<run_id>.tmp`, flushes it, and uses `os.replace`. `load()` validates with `CaptainRunState.model_validate_json`. Reject reuse of a `run_id` when the project digest differs.

- [ ] **Step 5: Checkpoint before and after every release**

Persist the planned immutable batches before releasing. On resume, load them instead of calling decomposition/alignment/enrichment again. After each successful release append the batch id and save. Raise `PartialReleaseError(run_id, released_batch_ids, failed_batch_id)` while retaining the checkpoint.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest -q tests/planning/test_run_store.py tests/planning/test_resume.py tests/planning/test_captain_pipeline.py`

Commit:

```powershell
git add agenten/planning/run_models.py agenten/planning/run_store.py agenten/planning/captain_pipeline.py agenten/planning/cli.py tests/planning
git commit -m "feat: resume partial captain planning runs"
```

---

### Task 5: Bound LLM stages with typed resilience policy

**Files:**
- Create: `agenten/llm/resilience.py`
- Modify: `agenten/planning/factory.py`
- Modify: `agenten/llm/plan_batches.py`
- Test: `tests/llm/test_resilience.py`
- Test: `tests/planning/test_factory_e2e.py`

**Interfaces:**
- Produces: `LlmStage`, `LlmStageError`, `LlmTimeoutError`, `LlmSchemaError`, and `run_llm_stage`.
- Policy: two attempts per stage, 30-second timeout per attempt, retry only timeout/transient provider failures, never retry deterministic planning-policy failures.

- [ ] **Step 1: Write failing timeout and retry tests**

```python
@pytest.mark.asyncio
async def test_stage_times_out_twice_then_raises_typed_error() -> None:
    async def blocked() -> str:
        await asyncio.Event().wait()
    with pytest.raises(LlmTimeoutError) as failure:
        await run_llm_stage(LlmStage.ENRICH, blocked, timeout_seconds=0.01, max_attempts=2)
    assert failure.value.attempts == 2


@pytest.mark.asyncio
async def test_schema_error_is_not_retried() -> None:
    calls = 0
    async def invalid() -> str:
        nonlocal calls
        calls += 1
        raise LlmSchemaError(LlmStage.ALIGN, "invalid structured output")
    with pytest.raises(LlmSchemaError):
        await run_llm_stage(LlmStage.ALIGN, invalid, timeout_seconds=1, max_attempts=2)
    assert calls == 1
```

- [ ] **Step 2: Verify failures**

Run: `python -m pytest -q tests/llm/test_resilience.py`

Expected: collection failure because resilience types do not exist.

- [ ] **Step 3: Implement the bounded wrapper**

```python
async def run_llm_stage(
    stage: LlmStage,
    operation: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float,
    max_attempts: int,
) -> T:
    transient_provider_errors = (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.RateLimitError,
        openai.InternalServerError,
    )
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.wait_for(operation(), timeout_seconds)
        except LlmSchemaError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            if attempt == max_attempts:
                raise LlmTimeoutError(stage, attempt) from exc
        except transient_provider_errors as exc:
            if attempt == max_attempts:
                raise LlmStageError(stage, attempt, "provider") from exc
    raise AssertionError("unreachable")
```

- [ ] **Step 4: Apply it to decomposition, alignment, and enrichment**

Wrap each injected callable in the factory. Convert missing/wrong structured content in `plan_batches.py` to `LlmSchemaError` with the correct stage. Keep alignment-policy retries separate from provider retries.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest -q tests/llm tests/planning/test_factory_e2e.py`

Commit:

```powershell
git add agenten/llm/resilience.py agenten/llm/plan_batches.py agenten/planning/factory.py tests/llm tests/planning/test_factory_e2e.py
git commit -m "feat: bound captain llm stages"
```

---

### Task 6: Segregate event publication from in-process subscription

**Files:**
- Modify: `agenten/runtime/event_bus.py`
- Modify: `agenten/runtime/autogen_bus.py`
- Modify: `agenten/orchestration/pipeline.py`
- Modify: typed constructors under `agenten/decomposition/`, `agenten/constitution/`, `agenten/spawning/`, `agenten/workers/`, `agenten/supervision/`, and `agenten/ledger_bridge/`
- Test: `tests/runtime/test_event_bus_capabilities.py`
- Test: `tests/test_autogen_bus_integration.py`

**Interfaces:**
- Produces: `EventPublisher` with `publish`, `InProcessEventBus` with `publish` plus `subscribe`, and explicit `SubscriptionRegistrar` for AutoGen type subscriptions.
- Removes: `AutoGenEventBus.subscribe()` and its runtime `NotImplementedError`.

- [ ] **Step 1: Write failing capability tests**

```python
def test_autogen_bus_exposes_publication_only() -> None:
    assert isinstance(AutoGenEventBus(fake_runtime()), EventPublisher)
    assert not hasattr(AutoGenEventBus(fake_runtime()), "subscribe")


def test_pipeline_rejects_publication_only_bus_at_composition_time() -> None:
    with pytest.raises(TypeError, match="in-process subscription capability"):
        build_pipeline(bus=AutoGenEventBus(fake_runtime()), llm_decompose=fake_decompose)
```

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -q tests/runtime/test_event_bus_capabilities.py tests/test_autogen_bus_integration.py`

Expected: AutoGen bus still exposes `subscribe` and fails only when invoked.

- [ ] **Step 3: Split the ports**

```python
@runtime_checkable
class EventPublisher(Protocol):
    async def publish(self, topic: str, event: object) -> None: ...


@runtime_checkable
class InProcessEventBus(EventPublisher, Protocol):
    def subscribe(self, topic: str, handler: Handler) -> None: ...
```

Use `EventPublisher` in agents that only publish. Require `InProcessEventBus` in `build_pipeline`, because that composition root registers callable handlers. Keep AutoGen type registration in `bootstrap.py` behind `SubscriptionRegistrar`.

- [ ] **Step 4: Remove the unsupported method and update architecture tests**

Delete `AutoGenEventBus.subscribe`. Replace old `pytest.raises(NotImplementedError)` assertions with construction-time capability assertions.

- [ ] **Step 5: Verify and commit**

Run:

```powershell
python -m pytest -q tests/runtime/test_event_bus_capabilities.py tests/test_autogen_bus_integration.py tests/test_e2e_smoke.py
python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py
```

Commit:

```powershell
git add agenten/runtime agenten/orchestration agenten/decomposition agenten/constitution agenten/spawning agenten/workers agenten/supervision agenten/ledger_bridge tests
git commit -m "refactor: segregate event bus capabilities"
```

---

### Task 7: Prove the live MariaDB and gateway contract

**Files:**
- Modify: `scripts/verify_delivery_stack.py`
- Create: `scripts/verify_captain_gateway.py`
- Modify: `docs/DEMO.md`
- Test: `tests/test_delivery_stack_docs.py`

**Interfaces:**
- Consumes: Captain-owned MariaDB service, `TEST_MARIADB_DSN`, gateway app, and the gateway planning client.
- Produces: one non-destructive verification command that proves persistence, fencing, holdout isolation, capability lookup, and idempotent Captain replay.

- [ ] **Step 1: Write the failing verifier contract test**

```python
def test_gateway_verifier_runs_all_required_checks() -> None:
    text = Path("scripts/verify_captain_gateway.py").read_text(encoding="utf-8")
    for marker in (
        "mariadb_roundtrip",
        "claim_fencing",
        "holdout_isolation",
        "capability_lookup",
        "captain_idempotent_replay",
    ):
        assert marker in text
```

- [ ] **Step 2: Verify it fails**

Run: `python -m pytest -q tests/test_delivery_stack_docs.py::test_gateway_verifier_runs_all_required_checks`

Expected: missing verifier script.

- [ ] **Step 3: Implement the verifier**

The script must require `TEST_MARIADB_DSN`, clear only test-scoped rows, run the five named checks through actual `MariaDBStorage` and the FastAPI app, print a JSON summary without credentials, and exit nonzero on any false check.

- [ ] **Step 4: Run the live gate**

Run:

```powershell
$env:TEST_MARIADB_DSN = $env:LEDGER_TEST_DSN
python -m pytest -q tests/blockchain/test_mariadb_storage.py tests/gateway/test_gateway.py
python scripts/verify_captain_gateway.py
```

Expected: all MariaDB/gateway tests pass with zero skips; verifier returns five `true` fields. If the DSN is unavailable, report the gate as unverified rather than green.

- [ ] **Step 5: Verify and commit**

Commit only after the live gate succeeds:

```powershell
git add scripts/verify_delivery_stack.py scripts/verify_captain_gateway.py docs/DEMO.md tests/test_delivery_stack_docs.py
git commit -m "test: prove live captain gateway contract"
```

---

### Task 8: Integrate the CLI, documentation, and dependency cleanup

**Files:**
- Modify: `main.py`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/WORKSTREAMS.md`
- Modify: `requirements.txt`
- Modify: `tests/test_main_cli.py`
- Modify: `tests/test_submission_docs.py`

**Interfaces:**
- Produces: `python main.py plan <project> ...` as the canonical Captain planning command and architecture prose matching enforced package boundaries.

- [ ] **Step 1: Write the failing CLI delegation test**

```python
def test_plan_command_delegates_to_captain_planning_cli(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project.md"
    project.write_text("Build a verified system", encoding="utf-8")
    called: list[list[str]] = []
    monkeypatch.setattr(main, "run_planning_cli", lambda args: called.append(args) or 0)
    assert main.main(["plan", str(project), "--capability", "delivery"]) == 0
    assert called[0][0] == str(project)
```

- [ ] **Step 2: Verify it fails**

Run: `python -m pytest -q tests/test_main_cli.py::test_plan_command_delegates_to_captain_planning_cli`

Expected: `plan` is not a recognized root command.

- [ ] **Step 3: Add the root command without duplicating parser logic**

Expose a callable from `agenten.planning.cli` and delegate the remaining arguments from `main.py`. Keep `demo` and `legacy` unchanged.

- [ ] **Step 4: Rewrite architecture around the real runtime path**

Document these exact flows:

```text
Planning: project → decompose → align → policy → enrich → policy → capability lookup → release
Runtime: events → constitution → coordinator → worker → supervisor/reaper → sole-writer recorder
Persistence: offline JSON adapters | production gateway → MariaDB
Adjacent products: Hermes and n8n consume contracts; they are not root-runtime packages
```

- [ ] **Step 5: Remove dependency duplication and acknowledge warnings**

Remove the duplicate `fastapi==0.139.0` line. Add a tracked compatibility issue/check for the `httpx`/Starlette TestClient deprecation and the `requests` dependency mismatch; do not silence either warning globally.

- [ ] **Step 6: Run the complete completion gate**

Run:

```powershell
python -m pytest -q
python scripts/verify_submission.py
python scripts/verify_captain_gateway.py
$demo = Join-Path $env:TEMP "captain-gap-remediation-demo.json"
python main.py demo --output $demo
Remove-Item -LiteralPath $demo
python -m compileall -q agenten blockchain chats config gateway
python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
git diff --check
git status --short --branch
```

Expected: full suite passes; gateway verifier is live-green; offline demo reports two completed subproblems; architecture gate passes; worktree is clean after commits. Report skipped tests and dependency warnings separately from failures.

- [ ] **Step 7: Commit**

```powershell
git add main.py README.md docs/ARCHITECTURE.md docs/WORKSTREAMS.md requirements.txt tests/test_main_cli.py tests/test_submission_docs.py
git commit -m "docs: complete captain remediation handoff"
```

---

## Implementation Order and Review Gates

1. Task 1 must land first so every implementation branch starts from the same baseline.
2. Tasks 2 and 3 establish the Captain↔Gateway contract; review them together before starting resume logic.
3. Task 4 owns durable project-run state and must not be mixed with delivery-runtime state under `agenten/delivery/`.
4. Task 5 is independent after Task 2 and may run in parallel with Task 4.
5. Task 6 is an isolated runtime refactor; merge it only after the complete event-driven E2E suite passes.
6. Task 7 is the evidence gate for Tasks 3 and 4; mocked tests cannot replace it.
7. Task 8 runs last and may update shared integration hotspots only after simulating merges with active branches.

## Self-Review

- **Gap coverage:** integration drift → Task 1; invented capabilities and holdout overlap → Task 2; missing gateway adapters → Task 3; partial release/project status → Task 4; LLM timeout/retry classification → Task 5; AutoGen subscription mismatch → Task 6; skipped live persistence evidence → Task 7; CLI/docs/dependency warnings → Task 8.
- **Scope:** no Hermes worker, Codex child-process, n8n workflow, or Minibook implementation is included.
- **Type consistency:** `GatewayPlanningClient` implements the existing `BatchReleaseClient` and `CapabilityResolver`; `CaptainRunStore` owns only planning-run checkpoints; event publication/subscription ports remain separate from storage.
- **Completion evidence:** the objective is not complete until the full suite, live MariaDB/gateway suite, offline demo, verifier, compileall, and architecture fitness gate all pass on the intended integration candidate.

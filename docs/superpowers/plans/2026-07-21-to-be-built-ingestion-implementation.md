# TO_BE_BUILT Ingestion and Captain Compilation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old per-build `input.md` assumption with a strict, immutable `TO_BE_BUILT.md` contract that Captain deterministically compiles into a versioned Factory job, public assertions, private holdout references, and an acyclic work graph.

**Architecture:** A byte-level loader validates the exact filename, UTF-8 encoding, required headings, nested agents/integrations, and forbidden credential values. A separate compiler converts the parsed document into frozen public contracts and sends private holdout bodies only to an injected Captain-owned store. A capability resolver decides reuse versus Forge without importing Minibook or Hermes. The old root `input.md` remains untouched; an explicit migration tool can generate a review candidate but cannot make it canonical automatically.

**Tech Stack:** Python 3.11, Pydantic v2, standard-library `hashlib`, `graphlib`, `pathlib`, `re`, pytest.

## Global Constraints

- This package owns `agenten/agent_factory/input_*`, `job_builder.py`, `contracts.py`, its fixtures, and its focused tests. Do not edit Minibook, Hermes, Gateway, release, or projection code.
- Read exact bytes and calculate SHA-256 before normalization. Reject UTF-8 decode errors, BOM ambiguity, NUL bytes, duplicate required headings, and a filename other than `TO_BE_BUILT.md`.
- Preserve additional Markdown sections as evidence. They cannot override required security, acceptance, authority, or stop sections.
- Credential aliases such as `CRM_API_KEY` are allowed; assignments, bearer tokens, private keys, URLs containing credentials, and likely secret values are rejected before any worker starts.
- Public compiler output contains holdout references and digests only. Private holdout bodies never enter a job, prompt, fixture, log, or Minibook event.
- Stable IDs derive from canonical semantic paths plus source digest, never list position alone.
- The compiler is deterministic and offline. No LLM, Hermes, Codex, n8n, Docker, browser, or network call is permitted.
- Keep `AgentFactoryJob.v1` readable for historical Gateway records; new builds emit `captain.agent-factory-job.v2` only.

---

## Task 1: Freeze Valid, Invalid, and Legacy Input Fixtures

**Files:**
- Create: `tests/fixtures/agent_factory/TO_BE_BUILT.valid.md`
- Create: `tests/fixtures/agent_factory/TO_BE_BUILT.missing-section.md`
- Create: `tests/fixtures/agent_factory/TO_BE_BUILT.credential-bearing.md`
- Create: `tests/fixtures/agent_factory/legacy-input.md`
- Modify: `tests/agent_factory/test_input_document.py`

**Interfaces:** Defines the smallest complete canonical request: two agents, one required external integration, one agent with no n8n need, one shared workflow, one public success case, security rules, acceptance outcomes, and stop conditions.

- [ ] **Step 1: Replace the repository-root dependency with explicit fixtures**

Write a failing test that copies the valid fixture to `tmp_path / "TO_BE_BUILT.md"` and asserts the loader exposes the source digest, byte length, title, all ten required sections, two agents, and one integration. Stop reading root `input.md` from tests.

- [ ] **Step 2: Add fail-closed fixture tests**

```python
@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("TO_BE_BUILT.missing-section.md", "missing required section"),
        ("TO_BE_BUILT.credential-bearing.md", "credential value"),
    ],
)
def test_invalid_input_is_rejected_before_compilation(fixture: str, reason: str) -> None:
    canonical_path = tmp_path / "TO_BE_BUILT.md"
    canonical_path.write_bytes((FIXTURES / fixture).read_bytes())
    with pytest.raises(FactoryInputError, match=reason):
        load_factory_input(canonical_path)
```

- [ ] **Step 3: Run the tests and verify the expected failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_document.py`

Expected: FAIL because the current loader requires `input.md` and has the old five-section schema.

- [ ] **Step 4: Commit fixtures and failing tests**

```powershell
git add tests/fixtures/agent_factory tests/agent_factory/test_input_document.py
git commit -m "test: define canonical to be built input"
```

## Task 2: Implement the Strict Byte-Level Input Contract

**Files:**
- Create: `agenten/agent_factory/input_contracts.py`
- Modify: `agenten/agent_factory/input_document.py`
- Modify: `agenten/agent_factory/__init__.py`
- Modify: `tests/agent_factory/test_input_document.py`

**Interfaces:** Produces `FactoryInputDocumentV2`, `InputSection`, `RequestedAgent`, `RequestedIntegration`, `RealCaseRequirement`, `CredentialAlias`, `FactoryInputError`, `load_factory_input(path)`, and `parse_factory_input_bytes(source, logical_name)`.

- [ ] **Step 1: Add strict model tests**

Test unknown fields, duplicate stable names, empty nested agent subsections, conflicting required/optional integration declarations, missing success case, cyclic handoff declarations, and non-uppercase credential aliases. Assert models are frozen and serialize byte-stably.

- [ ] **Step 2: Implement frozen contracts**

```python
class RequestedIntegration(_FrozenContract):
    integration_key: str = Field(pattern=IDENTIFIER_PATTERN)
    purpose: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    required: bool
    credential_aliases: tuple[str, ...] = ()
    success_behavior: str = Field(min_length=1)
    failure_behavior: str = Field(min_length=1)


class RequestedAgent(_FrozenContract):
    agent_key: str = Field(pattern=IDENTIFIER_PATTERN)
    purpose: str = Field(min_length=1)
    responsibilities: tuple[str, ...] = Field(min_length=1)
    input_schema_markdown: str = Field(min_length=1)
    output_schema_markdown: str = Field(min_length=1)
    handoffs: tuple[str, ...]
    prompt_requirements: tuple[str, ...] = Field(min_length=1)
    integration_keys: tuple[str, ...] = ()
    n8n_requirement: Literal["required", "not_required"]
    success_metrics: tuple[str, ...] = Field(min_length=1)
    real_cases: tuple[RealCaseRequirement, ...]
```

`FactoryInputDocumentV2` must include `input_ref`, `byte_length`, `source_name`, `title`, each of the ten required sections, parsed integrations/agents, preserved extra sections, and a `schema_name="captain.to-be-built.v1"` field. Use heading paths rather than global regex-only slicing so nested `## Agent:` and `###` blocks remain associated correctly.

- [ ] **Step 3: Implement exact-byte loading and secret rejection**

```python
def load_factory_input(path: Path) -> FactoryInputDocumentV2:
    if path.name != "TO_BE_BUILT.md":
        raise FactoryInputError("canonical factory input must be named TO_BE_BUILT.md")
    source_bytes = path.read_bytes()
    return parse_factory_input_bytes(source_bytes, logical_name=path.name)
```

Decode with strict UTF-8. Create the artifact digest from `source_bytes`, not re-encoded text. Reject high-confidence secret assignment patterns while allowing alias-only references. Error messages may name the line and alias but never echo the matched value.

- [ ] **Step 4: Run focused and architecture tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_document.py tests/test_import_boundaries.py
```

Expected: PASS, and `input_document.py` has no imports from Hermes, Minibook, Gateway, AutoGen, or infrastructure packages.

- [ ] **Step 5: Commit the loader**

```powershell
git add agenten/agent_factory/input_contracts.py agenten/agent_factory/input_document.py agenten/agent_factory/__init__.py tests/agent_factory/test_input_document.py
git commit -m "feat: parse canonical to be built input"
```

## Task 3: Add an Explicit, Non-Authoritative Legacy Migration Preflight

**Files:**
- Create: `agenten/agent_factory/input_migration.py`
- Create: `scripts/migrate_to_be_built.py`
- Create: `tests/agent_factory/test_input_migration.py`

**Interfaces:** Produces `InputMigrationReport`, `MigrationFinding`, `render_migration_candidate(source)`, and a CLI that writes only a caller-selected candidate path.

- [ ] **Step 1: Write non-overwrite and review-marker tests**

Test that the legacy sales-playbook fixture produces a candidate containing all ten headings plus preserved source citations, but the candidate contains `<!-- CAPTAIN_REVIEW_REQUIRED -->` and therefore fails canonical loading until a human resolves every finding. Test that an existing output path is rejected unless `--overwrite-candidate` is explicitly supplied; the canonical source path itself can never be the overwrite target.

- [ ] **Step 2: Run the migration tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_migration.py`

Expected: FAIL because the migration module does not exist.

- [ ] **Step 3: Implement deterministic heading mapping**

Map recognizable playbook areas (`Project Overview`, `Agents`, `Shared Workflows`, `Security`, `Success Metrics`, resources/links) into the new template. Unmapped requirements stay under `Helpful resources` with source heading paths. Every missing decision becomes a typed finding; never invent required/optional status, credentials, success or failure behavior, approval boundaries, or real-case expected outputs.

CLI example:

```powershell
.\.venv\Scripts\python.exe scripts/migrate_to_be_built.py --source .\TO_BE_BUILT.md --output .\artifacts\migration\TO_BE_BUILT.candidate.md
```

The command returns exit code `2` while review findings remain and prints only counts and the output path.

- [ ] **Step 4: Run migration and input regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_migration.py tests/agent_factory/test_input_document.py`

- [ ] **Step 5: Commit the migration tool**

```powershell
git add agenten/agent_factory/input_migration.py scripts/migrate_to_be_built.py tests/agent_factory/test_input_migration.py
git commit -m "feat: add to be built migration preflight"
```

## Task 4: Compile Public Assertions, Private Holdouts, and the Work DAG

**Files:**
- Create: `agenten/agent_factory/input_compiler.py`
- Create: `agenten/agent_factory/holdout_contracts.py`
- Create: `agenten/agent_factory/holdout_store.py`
- Create: `tests/agent_factory/test_input_compiler.py`

**Interfaces:** Produces `CompiledFactorySpecification`, `AcceptanceAssertion`, `FactoryWorkNode`, `PrivateHoldoutRef`, `PrivateHoldoutStore`, and `FactoryInputCompiler.compile(document, subject_version)`. The reference type lives in `holdout_contracts.py`; storage implementations never leak into public contracts.

- [ ] **Step 1: Write deterministic compilation tests**

```python
def test_compiler_emits_stable_assertions_holdout_refs_and_topological_dag() -> None:
    first = compiler().compile(document(), subject_version=1)
    second = compiler().compile(document(), subject_version=1)

    assert first == second
    assert first.source_ref == document().input_ref
    assert set(first.assertion_ids) == {item.assertion_id for item in first.assertions}
    assert all(ref.uri.startswith("holdout://") for ref in first.private_holdout_refs)
    assert tuple(TopologicalSorter(first.dependencies).static_order())
```

Add tests that business outcomes are preserved in assertion oracles, every integration and agent belongs to exactly one node, unknown handoffs and cycles fail, and the public result cannot serialize holdout bodies.

- [ ] **Step 2: Implement the public compilation contracts**

```python
class AcceptanceAssertion(_FrozenContract):
    assertion_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_path: tuple[str, ...] = Field(min_length=1)
    observable_setup: str = Field(min_length=1)
    observable_action: str = Field(min_length=1)
    observable_expected: str = Field(min_length=1)
    kind: Literal["business", "schema", "integration", "security", "recovery"]


class CompiledFactorySpecification(_FrozenContract):
    schema_name: Literal["captain.compiled-factory-spec.v1"]
    source_ref: ArtifactRef
    capability_key: str
    assertions: tuple[AcceptanceAssertion, ...]
    private_holdout_refs: tuple[PrivateHoldoutRef, ...]
    work_nodes: tuple[FactoryWorkNode, ...]
    dependency_order: tuple[str, ...]
```

Stable assertion IDs use `assert-<12 hex>` from source digest + semantic heading path + normalized observable outcome. Generate controlled-recovery and private-holdout bodies through an injected Captain-owned planner, persist them immediately in `PrivateHoldoutStore`, and expose only `holdout://<id>` plus SHA-256 in the compiled spec.

- [ ] **Step 3: Implement deterministic dependency rules**

Create nodes for architecture, each integration/tool decision, AutoGen implementation, required n8n workflow implementation, local adapter implementation, package assembly, real cases, quality, recovery, and release. A declared external integration creates an n8n decision node; an agent declaring `not_required` does not. Use `TopologicalSorter.prepare()` to fail on cycles.

- [ ] **Step 4: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_compiler.py tests/planning/test_policy.py
```

- [ ] **Step 5: Commit compilation**

```powershell
git add agenten/agent_factory/input_compiler.py agenten/agent_factory/holdout_contracts.py agenten/agent_factory/holdout_store.py tests/agent_factory/test_input_compiler.py
git commit -m "feat: compile factory assertions and work graph"
```

## Task 5: Version the Factory Job Without Breaking Historical Reads

**Files:**
- Modify: `agenten/agent_factory/contracts.py`
- Modify: `agenten/agent_factory/job_builder.py`
- Modify: `agenten/agent_factory/__init__.py`
- Create: `tests/fixtures/agent_factory/agent_factory_job.v2.json`
- Modify: `tests/agent_factory/test_contracts.py`
- Modify: `tests/agent_factory/test_job_builder.py`

**Interfaces:** Produces `AgentFactoryJobV2`, `FactoryJob = AgentFactoryJob | AgentFactoryJobV2`, and `build_factory_job(compiled, correlation_id, now, wall_clock_budget_seconds)`.

- [ ] **Step 1: Add v1 compatibility and v2 strictness tests**

Test that the existing v1 fixture still validates unchanged. The v2 fixture must bind source/spec/DAG refs, stable assertion IDs, holdout refs, creation deadline, capability key, and the fixed maximum of five behavioral iterations. Reject a deadline at/before `occurred_at`, a digest mismatch, and duplicate assertion/holdout IDs.

- [ ] **Step 2: Implement the v2 contract**

```python
class AgentFactoryJobV2(_FrozenContract):
    schema_name: Literal["captain.agent-factory-job.v2"] = Field(alias="schema", serialization_alias="schema")
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    occurred_at: datetime
    producer: Literal["captain"]
    job_id: UUID
    subject_version: int = Field(ge=1, strict=True)
    input_ref: ArtifactRef
    compiled_spec_ref: ArtifactRef
    dependency_graph_ref: ArtifactRef
    required_capability: str = Field(pattern=IDENTIFIER_PATTERN)
    acceptance_assertion_ids: tuple[str, ...] = Field(min_length=1)
    private_holdout_refs: tuple[PrivateHoldoutRef, ...] = Field(min_length=1)
    max_behavioral_iterations: Literal[5] = 5
    deadline_at: datetime
```

Use a discriminated `TypeAdapter` or explicit `parse_factory_job` helper at transport boundaries. Do not mutate the v1 schema name or reinterpret old fields.

- [ ] **Step 3: Change the job builder to consume only compiled input**

Remove positional `output-01`/`quality-gate-01` generation. Bind refs to canonical serialized compiled artifacts and derive deterministic `job_id`/`event_id` from correlation, input digest, and subject version so identical submissions replay.

- [ ] **Step 4: Run contracts, builder, and Gateway fixture regressions**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_contracts.py tests/agent_factory/test_job_builder.py tests/gateway/test_factory_repository.py
```

- [ ] **Step 5: Commit the job version**

```powershell
git add agenten/agent_factory/contracts.py agenten/agent_factory/job_builder.py agenten/agent_factory/__init__.py tests/fixtures/agent_factory/agent_factory_job.v2.json tests/agent_factory/test_contracts.py tests/agent_factory/test_job_builder.py
git commit -m "feat: version compiled factory jobs"
```

## Task 6: Resolve Capability Reuse Before Forge Submission

**Files:**
- Create: `agenten/agent_factory/capability_resolution.py`
- Create: `tests/agent_factory/test_capability_resolution.py`

**Interfaces:** Produces `CapabilityCatalogPort`, `CapabilityResolution`, `CapabilityResolver.resolve(job)`, with outcomes `reuse` or `create` only.

- [ ] **Step 1: Write catalog hit/miss tests**

Test exact capability/version/assertion compatibility, idempotent replay, incompatible schema, unresolved required gap, and stale capability version. A compatible hit returns a content-addressed `PromotedCapability` and never requests Forge; a miss returns a creation request keyed by job/input/spec digests.

- [ ] **Step 2: Implement the transport-neutral resolver**

```python
class CapabilityCatalogPort(Protocol):
    def compatible_capability(self, job: AgentFactoryJobV2) -> PromotedCapability | None: ...


class CapabilityResolver:
    def resolve(self, job: AgentFactoryJobV2) -> CapabilityResolution:
        capability = self._catalog.compatible_capability(job)
        if capability is not None:
            return CapabilityResolution(kind="reuse", capability=capability)
        return CapabilityResolution(kind="create", creation_key=_creation_key(job))
```

The Gateway adapter is Package C's responsibility. This module contains no HTTP, SQL, Minibook, or Hermes code.

- [ ] **Step 3: Run the complete Package A gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/agent_factory/test_input_document.py tests/agent_factory/test_input_migration.py tests/agent_factory/test_input_compiler.py tests/agent_factory/test_job_builder.py tests/agent_factory/test_capability_resolution.py
.\.venv\Scripts\python.exe -m compileall -q agenten\agent_factory
```

- [ ] **Step 4: Commit the resolver**

```powershell
git add agenten/agent_factory/capability_resolution.py tests/agent_factory/test_capability_resolution.py
git commit -m "feat: resolve factory capability reuse"
```

## Package A Handoff

Record the exact commit SHA, fixture digests, focused test count, and skipped tests. Packages B and C receive only:

- `captain.agent-factory-job.v2` fixture and schema behavior;
- the valid `TO_BE_BUILT.md` fixture;
- compiled-spec and dependency-graph artifact fixtures;
- the capability-resolution outcome fixture.

They do not receive private holdout bodies or access to `PrivateHoldoutStore`.

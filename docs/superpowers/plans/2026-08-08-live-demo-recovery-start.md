# Live Demo Recovery and Start Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to execute this plan task by task. Keep every RED/GREEN cycle and commit boundary intact.

**Goal:** Restore the current Captain capability runtime and provide one idempotent, fail-closed command that starts the free local demo infrastructure and, only with explicit authorization, runs three provider-backed capability builds within a USD 6 total ceiling.

**Architecture:** Keep the current event-driven Captain, Gateway, benchmark, retry, Minibook, and n8n contracts. Add a small digest-verified runtime-adapter loader, adapt the current Hermes/Codex/artifact implementations to the runtime ports, and compose existing lifecycle scripts behind `scripts/start-live-demo.ps1`. Historical live files are behavioral references only; no historical branch is merged wholesale.

**Tech stack:** Python 3.11, Pydantic, FastAPI/Uvicorn, AutoGen, PowerShell 7, Docker Compose, MariaDB, n8n REST/MCP, Minibook HTTP API, pytest.

**Global constraints:** Never print or commit credentials. Never mutate VibeMind-owned n8n or volumes. The default start must make zero paid provider calls. All external effects remain behind authenticated health, digest, lease, cost, release, and evidence gates. Mutable evidence stays below gitignored `.captain-cook/`.

---

## Task 1: Resolve Minibook from the canonical configured endpoint

**Files:**

- Modify: `scripts/minibook-demo.ps1`
- Modify: `tests/test_live_demo_operations.py`

- [ ] **Step 1: Replace the obsolete source assertion with a behavioral endpoint test**

Add a test that copies `minibook-demo.ps1` to a temporary repository fixture, supplies `MINIBOOK_BACKEND_URL=http://127.0.0.1:4456`, invokes `status`, and uses an injected probe seam to assert that only `http://127.0.0.1:4456/health` is queried. Add a second case with no environment override and a temporary `minibook/config.yaml` containing `port: 3556`; assert that `http://127.0.0.1:3556/health` is queried.

```python
def test_minibook_demo_prefers_configured_backend_url(tmp_path: Path) -> None:
    result = run_minibook_harness(
        tmp_path,
        env={"MINIBOOK_BACKEND_URL": "http://127.0.0.1:4456"},
        command="status",
    )
    assert result.returncode == 0
    assert "http://127.0.0.1:4456/health" in result.stdout
    assert ":8080" not in result.stdout
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_live_demo_operations.py -k "minibook_demo"
```

Expected: FAIL because the script still sets `$baseUrl` to port `8080` and has no injectable probe seam.

- [ ] **Step 3: Add a strict endpoint resolver**

Implement `Resolve-MinibookBaseUrl` in `scripts/minibook-demo.ps1` with this precedence:

1. non-empty `MINIBOOK_BACKEND_URL`;
2. `minibook/config.yaml` integer `port` converted to `http://127.0.0.1:<port>`;
3. fail with a recovery message identifying the missing setting/configuration.

Accept only loopback `http` URLs for this local helper. Trim the trailing slash. Add injectable `HealthProbe` and `IdentityProbe` scriptblocks with production defaults so tests do not start processes or call the network. Keep the existing managed-process identity checks.

```powershell
function Resolve-MinibookBaseUrl {
    param([string]$ConfiguredUrl, [string]$ConfigPath)
    if (-not [string]::IsNullOrWhiteSpace($ConfiguredUrl)) {
        $candidate = [uri]$ConfiguredUrl
    } else {
        $port = Get-MinibookConfiguredPort -ConfigPath $ConfigPath
        $candidate = [uri]"http://127.0.0.1:$port"
    }
    if ($candidate.Scheme -ne 'http' -or -not $candidate.IsLoopback) {
        throw 'Minibook demo endpoint must be a local HTTP URL.'
    }
    return $candidate.AbsoluteUri.TrimEnd('/')
}
```

- [ ] **Step 4: Add credential immutability coverage**

Add a behavioral test that snapshots a temporary `.env`, returns HTTP 401 from the identity probe, and asserts non-zero exit, redacted output, and byte-identical `.env`. Assert that the script does not call agent creation or bootstrap logic.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_live_demo_operations.py -k "minibook_demo"
```

Expected: PASS.

- [ ] **Step 6: Commit the endpoint correction**

```powershell
git add scripts/minibook-demo.ps1 tests/test_live_demo_operations.py
git commit -m "fix: resolve configured Minibook demo endpoint"
```

## Task 2: Add the free, idempotent live-demo start command

**Files:**

- Create: `scripts/start-live-demo.ps1`
- Create: `tests/test_start_live_demo.py`
- Modify: `.env.example`
- Modify: `docs/LIVE_DEMO_RUNBOOK.md`

- [ ] **Step 1: Write orchestration contract tests**

Create a PowerShell harness test suite with injectable `ServiceCommand`, `MinibookCommand`, and `CapabilityCommand` paths. Cover:

- default start calls configuration validation, Captain service start/status, and Minibook start/status, but never the capability command;
- a repeated default start uses the same managed identity returned by both child scripts;
- `-RunDemo` without confirmation or without a positive finite cost exits before any child command;
- `NaN`, infinity, zero, negative, and locale-ambiguous budget values are rejected;
- `-RunDemo -ConfirmProviderCost -MaxCostUsd 6.00` forwards exactly one total ceiling and invokes the capability command only after all health checks pass;
- child command failure prevents the green summary.

```python
@pytest.mark.parametrize("budget", [None, "0", "-1", "NaN", "Infinity", "6,00"])
def test_paid_demo_rejects_invalid_budget_before_children(
    start_script: Path, budget: str | None
) -> None:
    result = invoke_start_script(start_script, run_demo=True, confirm=True, budget=budget)
    assert result.returncode != 0
    assert "child-called" not in result.stdout
```

- [ ] **Step 2: Run tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_start_live_demo.py
```

Expected: FAIL because `scripts/start-live-demo.ps1` does not exist.

- [ ] **Step 3: Implement the canonical command**

Create `scripts/start-live-demo.ps1` with typed parameters:

```powershell
param(
    [switch]$RunDemo,
    [switch]$ConfirmProviderCost,
    [double]$MaxCostUsd,
    [string]$RecordingWindowTitle,
    [string]$ServiceCommand = (Join-Path $PSScriptRoot 'live-demo-services.ps1'),
    [string]$MinibookCommand = (Join-Path $PSScriptRoot 'minibook-demo.ps1'),
    [string]$CapabilityCommand = (Join-Path $PSScriptRoot 'run-capability-live-demo.ps1')
)
```

Validate every paid flag before invoking a child. Run `docker compose config` through the existing service helper before service starts. Invoke `live-demo-services.ps1 start`, then authenticated `status`; invoke `minibook-demo.ps1 start`, then credential-validating `status`. Print only service names, endpoints, stable process/container identities, and status. When paid execution is authorized, forward the invariant-culture `6.00` ceiling and optional recording title.

- [ ] **Step 4: Document configuration without secrets**

Add only variable names and local defaults to `.env.example`, including `MINIBOOK_BACKEND_URL`, `CAPTAIN_RUNTIME_URL`, `CAPTAIN_GATEWAY_URL`, `CAPTAIN_RUNTIME_ADAPTER_MANIFEST`, `CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256`, `CAPTAIN_CODEX_MODEL=gpt-5.6-terra`, and `CAPTAIN_CODEX_REASONING_EFFORT=high`. Update the runbook with the free command, explicit paid command, idempotency behavior, and credential recovery boundary.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_start_live_demo.py tests/test_live_demo_operations.py
```

Expected: PASS with no network or provider calls.

- [ ] **Step 6: Commit the start surface**

```powershell
git add scripts/start-live-demo.ps1 tests/test_start_live_demo.py .env.example docs/LIVE_DEMO_RUNBOOK.md
git commit -m "feat: add fail-closed live demo start command"
```

## Task 3: Load production runtime adapters from a digest-bound manifest

**Files:**

- Create: `agenten/agent_runtime/production_bootstrap.py`
- Create: `tests/agent_runtime/test_production_bootstrap.py`
- Modify: `agenten/agent_runtime/runtime_entrypoint.py`
- Modify: `agenten/agent_runtime/ports.py`

- [ ] **Step 1: Define failing manifest and port-validation tests**

Use temporary JSON manifests and adapter modules. Cover missing file, unsupported schema, SHA-256 mismatch, module outside the allowed repository/runtime root, missing factory, factory exception, incomplete binding, and successful binding. The success fixture returns fakes for `HermesPlannerPort`, `CodexExecutionPort`, and `ArtifactPort` and asserts `compose_gateway_backed_runtime_app` receives the exact instances.

```python
def test_load_runtime_adapters_rejects_digest_mismatch(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, sha256="0" * 64)
    with pytest.raises(RuntimeAdapterManifestError, match="digest"):
        load_runtime_adapters(manifest, expected_sha256="0" * 64)
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_runtime/test_production_bootstrap.py tests/agent_runtime/test_http_server.py
```

Expected: collection or import failure because `production_bootstrap.py` is absent.

- [ ] **Step 3: Make runtime ports runtime-checkable and define the binding**

Add `@runtime_checkable` to the three structural protocols in `ports.py`. Define immutable Pydantic manifest models and a frozen dataclass:

```python
@dataclass(frozen=True, slots=True)
class RuntimeAdapterBinding:
    hermes: HermesPlannerPort
    codex: CodexExecutionPort
    artifacts: ArtifactPort
```

The manifest schema is exactly `captain.runtime-adapters.v1` and contains `module_path`, `factory_name`, and `module_sha256`. The environment supplies `CAPTAIN_RUNTIME_ADAPTER_MANIFEST` and `CAPTAIN_RUNTIME_ADAPTER_MANIFEST_SHA256`. Verify the manifest digest before parsing and the adapter-module digest before importing. Resolve both paths and require them below the repository root or `.captain-cook/runtime-adapters/`.

- [ ] **Step 4: Replace the unconditional runtime failure**

Change `preflight_runtime()` to return a validated settings-and-binding object. In `main()`, create the authenticated Gateway client, call `compose_gateway_backed_runtime_app(...)` with the verified binding, and launch Uvicorn. No listener starts before manifest and module validation succeed.

```python
def preflight_runtime() -> RuntimeBootstrap:
    settings = RuntimeEntrypointSettings.from_env()
    binding = load_runtime_adapters_from_env(repository_root=_repository_root())
    return RuntimeBootstrap(settings=settings, binding=binding)
```

- [ ] **Step 5: Run focused and architecture tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_runtime/test_production_bootstrap.py tests/agent_runtime/test_http_server.py
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py
```

Expected: PASS.

- [ ] **Step 6: Commit the verified bootstrap**

```powershell
git add agenten/agent_runtime/production_bootstrap.py agenten/agent_runtime/runtime_entrypoint.py agenten/agent_runtime/ports.py tests/agent_runtime/test_production_bootstrap.py
git commit -m "feat: load verified production runtime adapters"
```

## Task 4: Adapt current Hermes, Codex, and artifact implementations to runtime ports

**Files:**

- Create: `agenten/agent_runtime/captain_production_adapters.py`
- Create: `tests/agent_runtime/test_captain_production_adapters.py`
- Modify: `agenten/agent_factory/hermes_cli.py`
- Modify: `agenten/agent_factory/codex_build_execution.py`

- [ ] **Step 1: Write behavioral port tests**

Use fake process runners and a temporary content-addressed store. Assert:

- Hermes planning returns the current typed plan and rejects an unreleased skill bundle;
- Codex streams JSONL events to the supplied observer during execution;
- exit code `124` is preserved as terminal timeout evidence and not rewritten as empty output;
- resume uses the same correlation and command identity;
- cancellation reaches the child process exactly once;
- artifact writes return a SHA-256 address and repeated identical content reuses it;
- provider bodies, prompts, and environment secrets never enter returned evidence.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_runtime/test_captain_production_adapters.py
```

Expected: FAIL because the adapter module does not exist.

- [ ] **Step 3: Implement thin adapters over current code**

Create `CaptainHermesPlannerAdapter`, `CaptainCodexExecutionAdapter`, and `ContentAddressedArtifactAdapter`. Delegate planning to `HermesCliFactory`, execution/recovery to the current `codex_build_execution` and `codex_build_recovery` seams, and persistence to a `.captain-cook/artifacts/sha256/` store. Do not import Gateway or blockchain infrastructure into these adapters.

Expose one factory function for the manifest loader:

```python
def create_runtime_adapters(context: RuntimeAdapterContext) -> RuntimeAdapterBinding:
    return RuntimeAdapterBinding(
        hermes=CaptainHermesPlannerAdapter.from_context(context),
        codex=CaptainCodexExecutionAdapter.from_context(context),
        artifacts=ContentAddressedArtifactAdapter(context.artifact_root),
    )
```

- [ ] **Step 4: Preserve streaming and timeout evidence in existing execution seams**

Make the smallest changes required for the observer callback to receive every parsed JSONL line while the process runs. Represent timeout `124` as a typed terminal result with elapsed duration, last event hash, and resumable checkpoint. Do not include raw model content in the persisted summary.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_runtime/test_captain_production_adapters.py tests/agent_factory/test_codex_build_execution.py tests/agent_factory/test_codex_build_recovery.py
```

Expected: PASS.

- [ ] **Step 6: Commit current-architecture production adapters**

```powershell
git add agenten/agent_runtime/captain_production_adapters.py agenten/agent_factory/hermes_cli.py agenten/agent_factory/codex_build_execution.py tests/agent_runtime/test_captain_production_adapters.py
git commit -m "feat: bind Captain production runtime ports"
```

## Task 5: Generate and validate the local adapter manifest

**Files:**

- Create: `scripts/generate-runtime-adapter-manifest.py`
- Create: `tests/agent_runtime/test_generate_runtime_adapter_manifest.py`
- Modify: `.gitignore`
- Modify: `scripts/live-demo-services.ps1`

- [ ] **Step 1: Write deterministic generation tests**

Assert the generator writes the manifest below `.captain-cook/runtime-adapters/`, hashes the exact committed adapter module bytes, emits schema `captain.runtime-adapters.v1`, writes no secret values, and produces byte-identical JSON across repeated runs. Assert `live-demo-services.ps1 start` refuses to launch Runtime when the manifest or expected digest is absent or mismatched.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_runtime/test_generate_runtime_adapter_manifest.py tests/test_live_demo_operations.py -k "adapter_manifest or runtime"
```

Expected: FAIL because the generator is absent and service startup cannot validate it.

- [ ] **Step 3: Implement deterministic generation and service wiring**

Generate canonical UTF-8 JSON with sorted keys and a trailing newline. Print only the manifest path and both digests. Extend `live-demo-services.ps1` to generate or validate the manifest before Runtime start, inject the two manifest environment names, wait for authenticated Runtime health, and retain managed PID identity. A pre-existing manifest is reused only when both digests match current bytes.

- [ ] **Step 4: Keep mutable adapter evidence ignored**

Add `.captain-cook/runtime-adapters/` explicitly to `.gitignore` even if the parent directory is already ignored, documenting the security boundary for reviewers.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_runtime/test_generate_runtime_adapter_manifest.py tests/test_live_demo_operations.py -k "adapter_manifest or runtime"
```

Expected: PASS.

- [ ] **Step 6: Commit manifest generation**

```powershell
git add scripts/generate-runtime-adapter-manifest.py scripts/live-demo-services.ps1 tests/agent_runtime/test_generate_runtime_adapter_manifest.py .gitignore
git commit -m "feat: generate digest-bound runtime adapter manifest"
```

## Task 6: Restore the three-input capability runner on current compositions

**Files:**

- Create: `agenten/agent_factory/capability_live_runner.py`
- Create: `agenten/agent_factory/capability_live_entrypoint.py`
- Create: `scripts/run-capability-live-demo.ps1`
- Create: `tests/agent_factory/test_capability_live_runner.py`
- Create: `tests/test_capability_live_demo_script.py`
- Modify: `gateway/agent_factory_live_composition.py`

- [ ] **Step 1: Write the final-evidence gate tests**

Create three canonical input fixtures in the test with distinct patterns:

1. sequential handoff with a required n8n integration;
2. selector/group-chat collaboration with conflicting input and human escalation;
3. swarm handoff with a missing API capability represented by a required-tool marker.

Assert that the runner rejects duplicated patterns, more or fewer than three inputs, a missing controlled recovery on the first input, fewer than three normal traces per capability, missing release/execution authority, missing Minibook read-back, unstable restart identity, or total booked cost above the supplied ceiling.

```python
def test_final_gate_requires_complete_evidence_for_each_capability() -> None:
    incomplete = complete_demo_evidence().model_copy(
        update={"projection_readback_id": None}
    )
    with pytest.raises(CapabilityLiveEvidenceError, match="projection read-back"):
        validate_live_demo_evidence(incomplete)
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_factory/test_capability_live_runner.py tests/test_capability_live_demo_script.py
```

Expected: FAIL because the runner and script do not exist.

- [ ] **Step 3: Compose current Captain and Gateway seams**

Implement `CapabilityLiveRunner` using the existing `LiveDemoRuntimeChain`, `AgentFactoryLiveComposition`, `AgentFactoryLiveOperator`, business benchmark production ports, Gateway release/execution authority, and Minibook projection feed. Do not recreate those domain decisions in the runner. Add only the narrowly required method to `AgentFactoryLiveComposition` when a typed existing seam is not externally callable.

Split each capability build into durable stages:

```text
scaffold_and_test_harness -> codex_implementation -> seal -> evaluate -> promote -> execute -> project
```

Persist a redacted checkpoint after every stage. A Runtime retry resumes the same suite and Hermes result rather than buying a new plan. Derive three equal per-input reservations from the total budget and assign any rounding remainder to the final input; the sum must never exceed `MaxCostUsd`.

- [ ] **Step 4: Add the PowerShell wrapper**

`scripts/run-capability-live-demo.ps1` validates the explicit confirmation and total budget again, selects the three repository-owned input files, calls the Python entrypoint, verifies the final evidence JSON, and optionally starts/stops FFmpeg for one exact named window. It must stop before provider execution if FFmpeg is absent or the title resolves to zero or multiple windows.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_factory/test_capability_live_runner.py tests/test_capability_live_demo_script.py tests/gateway/test_agent_factory_live_composition.py tests/gateway/test_agent_factory_live_operator.py
```

Expected: PASS without provider calls.

- [ ] **Step 6: Commit the live runner**

```powershell
git add agenten/agent_factory/capability_live_runner.py agenten/agent_factory/capability_live_entrypoint.py scripts/run-capability-live-demo.ps1 tests/agent_factory/test_capability_live_runner.py tests/test_capability_live_demo_script.py gateway/agent_factory_live_composition.py
git commit -m "feat: restore current capability live demo runner"
```

## Task 7: Add canonical demo inputs and immutable evidence verification

**Files:**

- Create: `examples/live-demo/sequential-n8n/TO_BE_BUILT.md`
- Create: `examples/live-demo/selector-review/TO_BE_BUILT.md`
- Create: `examples/live-demo/swarm-api-gap/TO_BE_BUILT.md`
- Create: `agenten/agent_factory/capability_live_evidence.py`
- Create: `tests/agent_factory/test_capability_live_evidence.py`
- Modify: `docs/LIVE_DEMO_RUNBOOK.md`

- [ ] **Step 1: Write input and evidence-schema tests**

Compile each input through the current input compiler and assert the selected AutoGen conversation pattern, integration intent, required-tool semantics, acceptance tests, and expected outcome differ. Add evidence-schema tests requiring stable IDs and hashes for decomposition, Hermes skill release, Codex command/result, three normal traces, controlled recovery, benchmark, Gateway decision/execution, Minibook projection/read-back, restart/resume, cost, duration, and model identity.

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_factory/test_capability_live_evidence.py tests/agent_factory/test_input_compiler.py
```

Expected: FAIL because the canonical inputs and evidence model are absent.

- [ ] **Step 3: Add production-shaped, non-secret inputs**

Write each `TO_BE_BUILT.md` with explicit input, expected outcome, constraints, tool requirements, escalation policy, deterministic test cases, and completion evidence. The n8n case requires an existing leased integration. The API-gap case requires a marker only when an indispensable API cannot be supplied; a self-hosted adapter option and a code-only fallback must be evaluated first.

- [ ] **Step 4: Implement immutable redacted evidence**

Use frozen Pydantic models with `extra='forbid'`. Persist canonical JSON below `.captain-cook/live-demo/<correlation-id>/final-evidence.json`; compute and return its SHA-256. Validators reject prompt text, provider response bodies, credential-shaped field names, and absolute paths.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/agent_factory/test_capability_live_evidence.py tests/agent_factory/test_input_compiler.py
```

Expected: PASS.

- [ ] **Step 6: Commit inputs and evidence contracts**

```powershell
git add examples/live-demo agenten/agent_factory/capability_live_evidence.py tests/agent_factory/test_capability_live_evidence.py docs/LIVE_DEMO_RUNBOOK.md
git commit -m "feat: add canonical live demo inputs and evidence"
```

## Task 8: Verify deterministic readiness, then perform the separately authorized live run

**Files:**

- Modify only if a test exposes a bounded defect in files introduced by Tasks 1-7.
- Evidence output: `.captain-cook/live-demo/` (gitignored, never staged)

- [ ] **Step 1: Run the focused integration set**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_start_live_demo.py tests/test_live_demo_operations.py tests/agent_runtime tests/agent_factory/test_capability_live_runner.py tests/agent_factory/test_capability_live_evidence.py tests/gateway/test_agent_factory_live_composition.py tests/gateway/test_agent_factory_live_operator.py
```

Expected: PASS.

- [ ] **Step 2: Run the complete deterministic gates**

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not live"
.\.venv\Scripts\python.exe scripts/verify_submission.py
.\.venv\Scripts\python.exe -m compileall -q agenten blockchain chats config gateway
.\.venv\Scripts\python.exe -m pytest -q --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
```

Expected: every command exits `0`. Report dependency warnings and skips separately from failures.

- [ ] **Step 3: Run the free infrastructure command twice**

```powershell
pwsh -NoProfile -File scripts/start-live-demo.ps1
pwsh -NoProfile -File scripts/start-live-demo.ps1
```

Expected: both commands exit `0`; the second reports the same managed process/container identities; authenticated n8n, Gateway, Runtime, and Minibook checks pass; no provider cost is booked.

- [ ] **Step 4: Stop for explicit paid-run authorization**

Present the deterministic and infrastructure evidence plus the exact command and maximum cost. Do not execute the next step until the user explicitly authorizes this live provider run in the active conversation.

- [ ] **Step 5: Execute the authorized USD 6 live run**

```powershell
pwsh -NoProfile -File scripts/start-live-demo.ps1 -RunDemo -ConfirmProviderCost -MaxCostUsd 6.00
```

Expected: three distinct completed capabilities; one controlled recovery; three normal traces per capability; Gateway promotion and execution; Minibook projection/read-back; restart/resume identity preservation; booked total `<= 6.00`.

- [ ] **Step 6: Validate immutable evidence and repository hygiene**

```powershell
git status --short --branch
git check-ignore .captain-cook/live-demo
```

Inspect the final evidence with the project validator, not by printing secret-bearing runtime files. Confirm no `.env`, credential, prompt, provider body, private case, or absolute user path is tracked.

- [ ] **Step 7: Commit only bounded verification corrections**

If deterministic verification required a code correction, rerun its RED/GREEN test and commit only that correction:

```powershell
git add <exact-files-from-the-bounded-correction>
git commit -m "fix: harden live demo verification"
```

If no correction was needed, create no verification-only commit.

## Definition of done

- [ ] Default start is authenticated, idempotent, and free.
- [ ] Paid execution is impossible without all three explicit arguments.
- [ ] Runtime starts only from a verified current adapter bundle.
- [ ] Three distinct conversation patterns build, test, evaluate, promote, execute, and project.
- [ ] Controlled recovery and restart/resume preserve identity.
- [ ] Evidence is complete, redacted, immutable, and ignored by Git.
- [ ] Deterministic, architecture, submission, and compile gates are green.
- [ ] The provider-backed evidence exists only after explicit authorization and remains within USD 6.

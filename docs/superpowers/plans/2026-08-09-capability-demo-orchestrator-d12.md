# Capability Demo Orchestrator D12 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide one reproducible, resumable operator lifecycle for the three canonical Captain capability inputs without caller-invented patterns, provider receipts, or live-success claims.

**Architecture:** A new Python package owns immutable bundle/env preflight, managed process identity, and the Captain HTTP lifecycle. A thin PowerShell wrapper exposes `init`, `start`, `status`, `run`, `resume`, and `stop`; every paid transition requires an explicit switch and a total ceiling no greater than USD 6.00. D10/D11 remain independent producers of the private benchmark catalog and sealed process bundle, so missing inputs fail before Docker, process, HTTP, or provider effects.

**Tech Stack:** Python 3.11, Pydantic 2, httpx, PowerShell 7, pytest, Windows process/listener inspection.

## Global Constraints

- Do not edit `gateway/capability_assembly_production.py`, `gateway/app.py`, or canonical demo inputs.
- Never print, commit, or persist raw provider output or credentials outside the gitignored private environment.
- `OPENAI_API_KEY` is operator-supplied and may enter only the provider-proxy child environment.
- `start` performs every file, digest, Compose, command, port, and authority preflight before the first mutation.
- `run` and `resume` make no provider-capable request without `-AllowPaidRun`; the total ceiling is at most USD 6.00.
- Only Captain/Gateway API responses supply lifecycle receipts. Caller-selected UUIDs are deterministic request identities explicitly permitted by the submit contract.
- `stop` terminates only PID/start/config identities written by this orchestrator and never removes Docker volumes.

---

### Task 1: Immutable configuration and safe initialization

**Files:**
- Create: `agenten/demo_orchestrator/contracts.py`
- Create: `agenten/demo_orchestrator/configuration.py`
- Create: `tests/demo_orchestrator/test_configuration.py`

**Interfaces:**
- Produces `DemoBundleV1`, `DemoServiceV1`, `DemoConfiguration`, `initialize_private_environment(...)`, and `load_demo_configuration(...)`.
- The sealed bundle contains the exact service order `gateway`, `provider_proxy`, `runtime`, `benchmark`, health URLs, listener ports, argv arrays, env allowlists, Compose inputs, canonical input digests, and Minibook evidence readback URL.

- [ ] Write failing tests proving init is atomic, preserves an existing private environment, never creates an OpenAI key, and emits only redacted output.
- [ ] Run `python -m pytest -q --no-cov tests/demo_orchestrator/test_configuration.py` and confirm the missing package failure.
- [ ] Implement strict parsing, SHA-256 validation, canonical input validation, distinct secret checks, and provider-only OpenAI env isolation.
- [ ] Re-run the focused tests and commit the configuration boundary.

### Task 2: Managed dependency-ordered service lifecycle

**Files:**
- Create: `agenten/demo_orchestrator/processes.py`
- Create: `tests/demo_orchestrator/test_processes.py`

**Interfaces:**
- Produces `ManagedProcessSupervisor.start_all(...)`, `restart_runtime(...)`, `status(...)`, and `stop(...)` over injected process, listener, health, and Compose ports.
- Persists PID, process start identity, command digest, configuration digest, and listener port after authenticated health and listener ownership agree.

- [ ] Write failing tests for complete preflight-before-mutation, Gateway-to-benchmark order, exact-config reuse, foreign-listener refusal, and managed-PID-only stop.
- [ ] Run the focused test and confirm behavior is absent.
- [ ] Implement immutable preflight records and managed lifecycle transitions.
- [ ] Re-run the focused tests and commit the service lifecycle.

### Task 3: Resumable Captain assembly HTTP client

**Files:**
- Create: `agenten/demo_orchestrator/client.py`
- Create: `tests/demo_orchestrator/test_client.py`

**Interfaces:**
- Produces `CapabilityDemoClient.submit()`, `run(resume=False)`, and `resume()`.
- Posts exactly three canonical paths, derives stable caller request identities from bundle/input digests, advances one slice at a time, persists every API checkpoint, restarts Runtime only after `resume_authorize`, and compares Gateway final evidence with Minibook readback.

- [ ] Write failing fake-HTTP tests for exact paths, no pattern field, `maximum_slices=1`, stable resume identity, controlled restart, redacted progress, bounded timeout, and exact final-evidence equality.
- [ ] Run the focused test and confirm behavior is absent.
- [ ] Implement the minimal authenticated HTTP/checkpoint loop with exit-code-124 timeout preservation.
- [ ] Re-run the focused tests and commit the HTTP lifecycle.

### Task 4: Operator CLI and PowerShell wrapper

**Files:**
- Create: `agenten/demo_orchestrator/cli.py`
- Create: `agenten/demo_orchestrator/__init__.py`
- Create: `scripts/capability-demo.py`
- Create: `scripts/capability-demo.ps1`
- Create: `tests/demo_orchestrator/test_cli.py`

**Interfaces:**
- `pwsh -NoProfile -File scripts/capability-demo.ps1 <init|start|status|run|resume|stop>` delegates to the repository package.
- Only `run`/`resume -AllowPaidRun -MaxCostUsd 6.00` enable provider-capable HTTP transitions.

- [ ] Write failing subprocess tests for all six actions, paid-run gating, ceiling validation, timeout 124, and secret-free output.
- [ ] Run the focused test and confirm the missing wrapper failure.
- [ ] Implement the thin entrypoints and stable exit-code mapping.
- [ ] Re-run all D12 tests and commit the operator surface.

### Task 5: Verification and operator documentation

**Files:**
- Create: `docs/capability-demo-orchestrator.md`
- Modify: `docs/superpowers/plans/2026-08-09-capability-demo-orchestrator-d12.md`

**Interfaces:**
- Documents the exact one-command lifecycle, D10/D11 preflight dependencies, paid authorization, recovery, evidence, and non-claims.

- [ ] Run D12 focused tests, architecture/import tests, compileall, submission verification, diff check, and credential scan.
- [ ] Record exact counts and known blocked live prerequisites without claiming Docker/provider/DB execution.
- [ ] Commit the verified documentation and hand off the branch without merging.


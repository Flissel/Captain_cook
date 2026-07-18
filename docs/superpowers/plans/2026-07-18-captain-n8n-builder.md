# Captain n8n Builder and Hermes Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver an isolated local Captain n8n builder with a generated local owner/API key and prepare the pinned Hermes runtime to consume only Captain-approved n8n configuration.

**Architecture:** A dedicated Compose file starts PostgreSQL and n8n under the `captain-n8n-builder` project on loopback port 5679, entirely separate from VibeMind. A strict endpoint resolver selects the builder only for `N8N_MODE=captain-builder`; Hermes receives a sanitized lease-derived configuration reference and never Docker control or a raw credential.

**Tech Stack:** Docker Compose, PostgreSQL 16, n8n `2.29.10`, PowerShell 7, Python 3.11, pytest, httpx, pinned Hermes submodule.

## Global Constraints

- `main` remains the integration source of truth; implement in this isolated branch and merge only after verification.
- Never start, stop, restart, inspect for workflow content, migrate, or mount `vibemind-n8n` or any VibeMind volume.
- Use only `docker compose -p captain-n8n-builder -f docker-compose.captain-n8n.yml`; never use `down -v`, `docker volume rm`, or Docker socket mounts.
- Secrets belong only in `.env.captain-n8n`; never print, commit, serialize, or put them in prompts, fixtures, test artifacts, or logs.
- Captain remains the sole issuer of `n8n-builder` capability grants. Hermes accepts a lease-derived configuration but cannot select arbitrary endpoints or self-grant access.
- Keep the Hermes submodule at its reviewed pinned commit; this plan adds parent-side readiness only and does not commit inside the submodule.

---

### Task 1: Add an isolated Captain n8n Compose contract

**Files:**
- Create: `docker-compose.captain-n8n.yml`
- Create: `.env.captain-n8n.example`
- Modify: `.gitignore`
- Create: `tests/test_captain_n8n_stack.py`

**Interfaces:**
- Consumes: environment keys `CAPTAIN_N8N_PORT`, `CAPTAIN_N8N_ENCRYPTION_KEY`, `CAPTAIN_N8N_POSTGRES_PASSWORD`, `CAPTAIN_N8N_POSTGRES_USER`, and `CAPTAIN_N8N_POSTGRES_DB`.
- Produces: a Compose project named `captain-n8n-builder` with services `postgres` and `n8n`, a loopback n8n endpoint, and only `captain_n8n_*` volumes.

- [ ] **Step 1: Write the isolation contract test**

```python
def test_captain_builder_compose_isolated_from_vibemind() -> None:
    compose = (ROOT / "docker-compose.captain-n8n.yml").read_text(encoding="utf-8")
    assert "name: captain-n8n-builder" in compose
    assert '127.0.0.1:${CAPTAIN_N8N_PORT:-5679}:5678' in compose
    assert "DB_TYPE=postgresdb" in compose
    assert "docker.sock" not in compose
    assert "vibemind" not in compose.lower()
    assert "external: true" not in compose
    assert "captain_n8n_postgres_data" in compose
    assert "captain_n8n_data" in compose
```

- [ ] **Step 2: Run the test to verify the missing contract**

Run: `python -m pytest -q tests/test_captain_n8n_stack.py`

Expected: FAIL because the Compose file does not exist.

- [ ] **Step 3: Create the Compose and environment examples**

```yaml
name: captain-n8n-builder
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: ${CAPTAIN_N8N_POSTGRES_DB:?missing}
      POSTGRES_USER: ${CAPTAIN_N8N_POSTGRES_USER:?missing}
      POSTGRES_PASSWORD: ${CAPTAIN_N8N_POSTGRES_PASSWORD:?missing}
    volumes: [captain_n8n_postgres_data:/var/lib/postgresql/data]
  n8n:
    image: n8nio/n8n:2.29.10
    environment:
      DB_TYPE: postgresdb
      DB_POSTGRESDB_HOST: postgres
      N8N_ENCRYPTION_KEY: ${CAPTAIN_N8N_ENCRYPTION_KEY:?missing}
    ports: [127.0.0.1:${CAPTAIN_N8N_PORT:-5679}:5678]
    volumes: [captain_n8n_data:/home/node/.n8n]
volumes:
  captain_n8n_postgres_data: {}
  captain_n8n_data: {}
```

Add all required PostgreSQL connection variables, healthchecks, timezone,
`N8N_HOST=localhost`, `N8N_PROTOCOL=http`, `WEBHOOK_URL`, and execution-data
pruning. Add `.env.captain-n8n` to `.gitignore`; leave `.env` and external
`N8N_MODE=external` behavior unchanged.

- [ ] **Step 4: Validate config and test the contract**

Run:

```powershell
docker compose -p captain-n8n-builder --env-file .env.captain-n8n.example -f docker-compose.captain-n8n.yml config
python -m pytest -q tests/test_captain_n8n_stack.py
```

Expected: Compose renders two services and the test passes without starting a
container.

- [ ] **Step 5: Commit the isolated stack contract**

```powershell
git add docker-compose.captain-n8n.yml .env.captain-n8n.example .gitignore tests/test_captain_n8n_stack.py
git commit -m "feat: add isolated captain n8n stack"
```

### Task 2: Implement safe lifecycle and owner/API bootstrap

**Files:**
- Create: `scripts/captain-n8n.ps1`
- Create: `scripts/verify_captain_n8n.ps1`
- Create: `tests/test_captain_n8n_scripts.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `scripts/captain-n8n.ps1 -Action init|start|bootstrap|status|stop`.
- Produces: a gitignored `.env.captain-n8n`, healthy Captain-owned resources,
  the `captain@local` owner, and `CAPTAIN_N8N_API_KEY` only in that environment file.

- [ ] **Step 1: Write static and argument-construction tests**

```python
def test_lifecycle_script_scopes_every_compose_call() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")
    assert "-p captain-n8n-builder" in source
    assert "-f $ComposeFile" in source
    assert "down -v" not in source.lower()
    assert "vibemind-n8n" not in source.lower()
    assert "captain@local" in source
    assert "ConvertTo-SecureString" in source

def test_bootstrap_never_echoes_secret_values() -> None:
    source = (ROOT / "scripts" / "captain-n8n.ps1").read_text(encoding="utf-8")
    assert "Write-Host $ApiKey" not in source
    assert "Write-Output $OwnerPassword" not in source
```

- [ ] **Step 2: Run the tests to verify the script is absent**

Run: `python -m pytest -q tests/test_captain_n8n_scripts.py`

Expected: FAIL because the lifecycle script does not exist.

- [ ] **Step 3: Implement secret initialization and safe Compose operations**

Implement `New-RandomSecret` with `RandomNumberGenerator::Create()` and
base64url output. Implement `Set-EnvValue` to create/update only
`.env.captain-n8n`, then use:

```powershell
& docker compose -p captain-n8n-builder --env-file $EnvFile -f $ComposeFile up -d --wait
```

Before this command, bind a `System.Net.Sockets.TcpListener` to
`127.0.0.1:$Port` and throw if it cannot bind. `stop` must use `stop`, not
`down`, and must pass the same project and Compose file. `status` must filter
only the exact project label `com.docker.compose.project=captain-n8n-builder`.

For bootstrap, wait for `/healthz`, call the pinned-image first-run owner
setup endpoint with a JSON body containing `email`, `firstName`, `lastName`,
and generated password, authenticate as the created owner, then create/verify
one labelled API key. Store only the returned key in `CAPTAIN_N8N_API_KEY`.
Treat an already configured owner and an existing key as idempotent success.
If n8n returns a schema other than the pinned setup/API-key response, stop
with an actionable error and retain the running Captain stack; do not mutate
PostgreSQL directly.

- [ ] **Step 4: Implement authenticated verification and operations docs**

`verify_captain_n8n.ps1` must check the expected project inventory, `/healthz`,
and an authenticated `GET /api/v1/workflows` using `X-N8N-API-KEY`; it outputs
only endpoint identity and status codes. Add README commands for init, start,
bootstrap, status, and stop, explicitly stating VibeMind is untouched.

- [ ] **Step 5: Run script parsing and contract tests**

Run:

```powershell
powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/captain-n8n.ps1))"
powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/verify_captain_n8n.ps1))"
python -m pytest -q tests/test_captain_n8n_stack.py tests/test_captain_n8n_scripts.py
```

Expected: both scripts parse and all focused tests pass.

- [ ] **Step 6: Commit lifecycle and bootstrap**

```powershell
git add scripts/captain-n8n.ps1 scripts/verify_captain_n8n.ps1 tests/test_captain_n8n_scripts.py README.md
git commit -m "feat: bootstrap captain n8n owner"
```

### Task 3: Make n8n endpoint selection explicit and lease-bound

**Files:**
- Create: `agenten/agent_runtime/n8n_endpoint.py`
- Modify: `agenten/targets/n8n.py`
- Modify: `.env.example`
- Create: `tests/agent_runtime/test_n8n_endpoint.py`
- Modify: `tests/targets/test_n8n_target.py`

**Interfaces:**
- Consumes: `resolve_n8n_endpoint(environment: Mapping[str, str]) -> N8nEndpoint`.
- Produces: validated `api_base_url`, `webhook_base_url`, and secret-excluded
  `api_key`; accepts the builder only with `N8N_MODE=captain-builder`.

- [ ] **Step 1: Write fail-closed selection tests**

```python
def test_builder_mode_uses_only_captain_values() -> None:
    endpoint = resolve_n8n_endpoint({
        "N8N_MODE": "captain-builder",
        "CAPTAIN_N8N_URL": "http://localhost:5679",
        "CAPTAIN_N8N_API_KEY": "secret",
    })
    assert endpoint.api_base_url == "http://localhost:5679"
    assert "secret" not in repr(endpoint)

def test_builder_mode_rejects_vibemind_url() -> None:
    with pytest.raises(N8nEndpointConfigurationError):
        resolve_n8n_endpoint({
            "N8N_MODE": "captain-builder",
            "CAPTAIN_N8N_URL": "http://localhost:15678",
            "CAPTAIN_N8N_API_KEY": "secret",
        })
```

- [ ] **Step 2: Run tests to confirm the resolver is missing**

Run: `python -m pytest -q tests/agent_runtime/test_n8n_endpoint.py`

Expected: FAIL on `ModuleNotFoundError`.

- [ ] **Step 3: Implement immutable endpoint configuration**

Create a frozen Pydantic model whose `api_key` field uses `repr=False` and
`exclude=True`. Accept only `external` and `captain-builder`, require loopback
HTTP URLs without userinfo for builder mode, require a non-empty builder API
key, and reject any builder URL that uses port `15678`. Keep external behavior
compatible with existing `N8N_URL` and `N8N_MCP_TOKEN` settings.

Add `N8nHttpClient.from_endpoint(endpoint, http)` and leave its HTTP evidence
semantics unchanged. Update `.env.example` to document the opt-in builder
variables without changing the external default.

- [ ] **Step 4: Derive Hermes configuration from a grant, not raw environment**

Add `build_hermes_n8n_reference(grant, endpoint) -> HermesN8nReference` in
the same module. It must call `validate_grant`, require the exact
`N8N_BUILDER` profile and `("n8n-mcp",)` server list, return only endpoint
identity and server name in its serializable model, and expose the key only
through an excluded child-process environment accessor. Add tests for expired,
plain-builder, wrong-server, and `repr`/JSON redaction cases.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
python -m pytest -q tests/agent_runtime/test_n8n_endpoint.py tests/agent_runtime/test_contracts.py tests/agent_runtime/test_prompt_policy.py tests/targets/test_n8n_target.py
```

Expected: all tests pass with no key text in captured representations.

- [ ] **Step 6: Commit endpoint and Hermes configuration boundary**

```powershell
git add agenten/agent_runtime/n8n_endpoint.py agenten/targets/n8n.py .env.example tests/agent_runtime/test_n8n_endpoint.py tests/targets/test_n8n_target.py
git commit -m "feat: bind captain n8n endpoint to runtime leases"
```

### Task 4: Verify the pinned Hermes runtime is ready for Captain envelopes

**Files:**
- Create: `scripts/verify_hermes_readiness.ps1`
- Create: `tests/contracts/test_hermes_runtime_readiness.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: initialized `hermes-agent` at the parent-pinned gitlink and a
  `HermesN8nReference` produced by Task 3.
- Produces: a redacted readiness report containing only the Hermes commit,
  available entrypoint names, test status, and n8n server identity.

- [ ] **Step 1: Write the readiness tests**

```python
def test_pinned_hermes_runtime_exposes_required_surfaces() -> None:
    assert (HERMES / "hermes_cli" / "captain_planner.py").is_file()
    assert (HERMES / "hermes_cli" / "mcp_config.py").is_file()
    assert (HERMES / "tests" / "fixtures" / "captain_work_package_released.v1.json").is_file()
    assert gitlink_commit(HERMES) == pinned_parent_gitlink(ROOT, "hermes-agent")
```

- [ ] **Step 2: Run the test to establish the required fixture/entrypoint expectation**

Run: `python -m pytest -q tests/contracts/test_hermes_runtime_readiness.py`

Expected: FAIL until the verifier and test are added.

- [ ] **Step 3: Implement the parent-only verifier**

Implement the script using `git submodule status --recursive`, `python -m
pytest -q` against only known Hermes focused tests, and `python -c` imports for
`hermes_cli.captain_planner` and `hermes_cli.mcp_config`. It must explicitly
fail if the submodule has a leading `-`, `+`, dirty status, detached wrong
commit, absent fixture, or missing entrypoint. Do not call Docker, start n8n,
or edit the submodule.

- [ ] **Step 4: Run parent compatibility and Hermes readiness gates**

Run:

```powershell
python -m pytest -q tests/contracts/test_work_package_compatibility.py tests/contracts/test_hermes_runtime_readiness.py
powershell -ExecutionPolicy Bypass -File scripts/verify_hermes_readiness.ps1
```

Expected: the report names `a5199779876455ece6aa7c1220de70bf3f62ece2`, no
secret values, and all focused tests pass.

- [ ] **Step 5: Commit the readiness gate**

```powershell
git add scripts/verify_hermes_readiness.ps1 tests/contracts/test_hermes_runtime_readiness.py README.md
git commit -m "test: verify hermes runtime readiness"
```

### Task 5: Prove a real isolated Captain n8n delivery without VibeMind impact

**Files:**
- Create: `tests/live/test_captain_n8n_builder_live.py`
- Modify: `tests/live/test_gate_a_codex_n8n.py`
- Modify: `scripts/verify_captain_n8n.ps1`

**Interfaces:**
- Consumes: a healthy Captain builder, `CAPTAIN_N8N_API_KEY`, an isolated
  `TEST_MARIADB_DSN`, `OPENAI_API_KEY`, and a real Codex CLI.
- Produces: a real Captain-namespaced workflow/execution and gateway evidence;
  cleanup may delete only the created Captain workflow.

- [ ] **Step 1: Write the live preflight and VibeMind invariance tests**

```python
@pytest.mark.live
async def test_captain_builder_has_real_api_and_preserves_vibemind() -> None:
    before = inspect_container("vibemind-n8n")
    endpoint = resolve_n8n_endpoint(load_captain_builder_environment())
    assert await health(endpoint.api_base_url) == 200
    assert await list_workflows(endpoint) is not None
    assert inspect_container("vibemind-n8n") == before
```

- [ ] **Step 2: Run the test to verify the live gate is absent**

Run: `python -m pytest -q -m live tests/live/test_captain_n8n_builder_live.py -rs`

Expected: FAIL because the test module does not exist. A missing local builder
prerequisite is reported as a skip, never as a pass.

- [ ] **Step 3: Implement real deployment and precise cleanup**

Reuse `N8nTarget` to deploy a webhook plus Respond-to-Webhook workflow under a
unique `captain::live-smoke::` namespace. Capture workflow ID, execution ID,
artifact digest, correlation ID, and gateway delivery-event reference. In a
`finally` block, delete only the exact recorded Captain workflow ID using the
Captain builder API. Do not enumerate, write, or delete VibeMind workflows.

Update Gate A to resolve the explicit configured endpoint so that it reports
the target identity but never its token. The normal live path must require
real API responses, real Codex output, and real MariaDB Gateway evidence.

- [ ] **Step 4: Run all quality gates**

Run:

```powershell
python -m pytest -q tests/test_captain_n8n_stack.py tests/test_captain_n8n_scripts.py tests/agent_runtime/test_n8n_endpoint.py tests/contracts/test_hermes_runtime_readiness.py
python -m pytest -q -m "not live"
python -m pytest -q -m live tests/live/test_captain_n8n_builder_live.py tests/live/test_gate_a_codex_n8n.py -rs
python scripts/verify_submission.py
python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py
python -m compileall -q agenten blockchain chats config gateway
git diff --check
```

Expected: all non-live tests pass; live tests either pass with real evidence or
state their missing external prerequisite separately. Never claim a skip,
mocked HTTP result, or container healthcheck as a successful delivery gate.

- [ ] **Step 5: Commit the live evidence gate and handoff**

```powershell
git add tests/live/test_captain_n8n_builder_live.py tests/live/test_gate_a_codex_n8n.py scripts/verify_captain_n8n.ps1
git commit -m "test: prove isolated captain n8n delivery"
```

## Plan self-review

- Scope coverage: Tasks 1-2 deliver an isolated Builder and local account;
  Task 3 enforces Captain-only endpoint/lease selection; Task 4 prepares the
  pinned Hermes boundary; Task 5 provides real evidence and VibeMind
  invariance.
- Placeholder scan: no deferred implementation markers are present; every
  task lists concrete paths, interfaces, test assertions, commands, and a
  commit boundary.
- Type consistency: `N8nEndpoint` and `HermesN8nReference` are introduced in
  Task 3 before Task 4 and Task 5 consume them; no submodule implementation
  is required for the parent-side readiness gate.

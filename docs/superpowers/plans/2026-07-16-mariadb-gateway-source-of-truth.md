# MariaDB Gateway Source-of-Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Program routing:** Do not dispatch this plan standalone. Follow
> `2026-07-16-remediation-program-orchestration.md`. Gateway Tasks 2-5 remain
> canonical; shared baseline, CI, health, and documentation work is consolidated
> into program packets owned by the orchestrator.

**Goal:** Make the MariaDB-backed FastAPI gateway the only production writer and source of delivery lifecycle truth, while preserving the deterministic JSON offline demo.

**Architecture:** Captain planning and delivery code use typed HTTP ports; only `gateway/` opens a MariaDB connection. A work batch remains immutable after creation. Claims, heartbeats, worker evidence, and terminal outcomes are append-only child blocks, and gateway queries derive the current batch projection from those events. The existing SQLite delivery ledger is migrated behind a gateway client and is no longer started or documented as a production control plane.

**Tech Stack:** Python 3.11, FastAPI 0.139.0, Pydantic 2.13.4, httpx 0.28.1, PyMySQL 1.2.0, MariaDB 11.8, pytest.

## Global Constraints

- Start every implementation branch from the current `main` in its own worktree; do not develop this plan on `feat/householder-runtime` or a stale plan branch.
- `gateway/` is the sole MariaDB writer. No module under `agenten/`, `blockchain/`, `chats/`, or `config/` may instantiate `MariaDBStorage` or execute SQL for delivery state.
- The JSON `JsonDirectoryReleaseClient` remains the explicitly selected offline/demo adapter; it is not a second production source of truth.
- Do not persist a plaintext claim token. Persist only its SHA-256 digest in an immutable claim event and return the plaintext token only in the claim response.
- Keep holdout cases hidden from list, bundle, capability, error, and model-facing responses until the valid claim and Codex-session conditions are met.
- Do not add production authentication secrets to source, fixtures, artifacts, or committed `.env` files. Tests generate their own token values.
- Every behavior change follows RED → GREEN → REFACTOR and ends with a narrow Conventional Commit.
- A MariaDB-specific acceptance command is a failure when it skips; a missing local DSN is not evidence that the gateway works.

---

## File Structure

- `gateway/contracts.py` — request/event models and the pure projection functions used by the gateway.
- `gateway/app.py` — authenticated HTTP boundary, append-only gateway store, and read projections; no mutable lifecycle `UPDATE` operations.
- `agenten/planning/gateway_client.py` — Captain release and capability adapters over the gateway HTTP contract.
- `agenten/planning/factory.py` and `agenten/planning/cli.py` — explicit offline versus gateway composition.
- `agenten/delivery/gateway_client.py` — delivery-state client over the same HTTP contract; it replaces production use of `SqliteDeliveryLedger`.
- `agenten/delivery/api.py` and `agenten/delivery/service.py` — gateway-proxy compatibility boundary, then removal of SQLite startup from the production entry point.
- `scripts/migrate_sqlite_delivery_ledger.py` — one-way, idempotent, dry-run-first import of old local events into gateway event blocks.
- `scripts/start_delivery_stack.ps1`, `docker-compose.yml`, `.env.example` — local gateway configuration and lifecycle.
- `tests/gateway/`, `tests/planning/`, `tests/delivery/`, `tests/test_architecture_fitness.py`, and `.github/workflows/ci.yml` — contract, migration, boundary, and real-MariaDB gates.

## Task 1: Freeze the canonical boundary and reject direct production storage

**Files:**
- Modify: `docs/WORKSTREAMS.md`
- Modify: `docs/superpowers/plans/2026-07-15-architecture-gap-todos.md`
- Modify: `tests/test_architecture_fitness.py`
- Modify: `tests/test_workstream_docs.py`

**Interfaces:**
- Consumes: the current local `main`, the `gateway/` service, and `MariaDBStorage`.
- Produces: a machine-checked rule that only the gateway package may construct `MariaDBStorage`, plus the fixed merge order for this plan.

- [x] **Step 1: Write the failing boundary and workstream assertions**

```python
def test_only_gateway_may_construct_mariadb_storage() -> None:
    violations = find_symbol_references(
        ROOT,
        symbol="MariaDBStorage",
        allowed_paths=("gateway/", "tests/", "scripts/migrate_sqlite_delivery_ledger.py"),
    )
    assert violations == []


def test_workstreams_name_mariadb_gateway_as_delivery_truth() -> None:
    plan = Path("docs/WORKSTREAMS.md").read_text(encoding="utf-8")
    assert "MariaDB gateway is the sole production delivery source of truth" in plan
    assert "SQLite delivery ledger is a production control plane" not in plan
```

- [x] **Step 2: Verify the assertions fail against the current documents/rules**

Run: `python -m pytest -q tests/test_architecture_fitness.py tests/test_workstream_docs.py`

Expected: FAIL because no symbol-level `MariaDBStorage` ownership rule and no canonical source-of-truth statement exist.

- [x] **Step 3: Add the documented ownership and integration sequence**

Add this exact rule to `docs/WORKSTREAMS.md`:

```markdown
`main` is the integration baseline. The MariaDB gateway is the sole production
delivery source of truth and sole MariaDB writer. The SQLite delivery ledger is
legacy-import input only; new production delivery code talks to the gateway.

Integration order: append-only gateway contract → Captain/delivery clients →
migration and operations → MariaDB CI gate → public documentation.
```

Add a symbol-reference helper in `tests/architecture_fitness.py` that parses `ast.Name` and `ast.Attribute` nodes, and make the new architecture test allow only `gateway/`, `tests/`, and the one migration script named above.

- [x] **Step 4: Verify and commit**

Run: `python -m pytest -q tests/test_architecture_fitness.py tests/test_workstream_docs.py`

Expected: PASS.

```powershell
git add docs/WORKSTREAMS.md docs/superpowers/plans/2026-07-15-architecture-gap-todos.md tests/architecture_fitness.py tests/test_architecture_fitness.py tests/test_workstream_docs.py
git commit -m "docs: declare gateway delivery source of truth"
```

## Task 2: Replace mutable gateway lifecycle state with append-only events

> **Program routing:** Execute this task as P07A (pure contracts/projection)
> followed by P07B (MariaDB-backed event-only store). P07C separately absorbs
> Captain Task 3 Step 4's idempotent release rule. Do not dispatch this task as
> one shared-file session.

**Files:**
- Create: `gateway/contracts.py`
- Create: `gateway/store.py`
- Create: `tests/gateway/test_contracts.py`
- Modify: `gateway/app.py`
- Modify: `tests/gateway/test_gateway.py`
- Modify: `tests/blockchain/test_mariadb_storage.py`

**Interfaces:**
- Consumes: immutable `work_batch` and `holdout` blocks plus child lifecycle blocks.
- Produces: `BatchProjection`, `ClaimEvent`, `HeartbeatEvent`, `EvidenceEvent`, and `BatchDoneEvent`; `GatewayStore.batch_projection(batch_id) -> BatchProjection`.

- [ ] **Step 1: Write failing append-only acceptance tests**

```python
def test_claim_and_completion_append_events_without_mutating_work_batch(client, storage) -> None:
    parent_index = create_batch(client)
    token = claim(client)
    done = worker_block(client, token, block_type="batch_done", status="succeeded", data={
        "batch_id": "batch-1", "outcome": "succeeded", "artifact_ref": "workflow-42",
        "capabilities": ["email"], "eval_score": 9,
    })
    assert done.status_code == 201

    blocks = storage.load()
    batch = next(block for block in blocks if block["index"] == parent_index)
    lifecycle = [block for block in blocks if block["parent_index"] == parent_index]
    assert batch["status"] == "pending"
    assert batch["children"] == []
    assert [block["block_type"] for block in lifecycle] == ["batch_claimed", "batch_done"]
    assert client.get("/batches/batch-1").json()["status"] == "succeeded"


def test_gateway_does_not_issue_lifecycle_update_sql() -> None:
    source = Path("gateway/app.py").read_text(encoding="utf-8")
    assert "UPDATE blocks SET status" not in source
    assert "UPDATE blocks SET metadata" not in source
    assert "UPDATE blocks SET children" not in source
```

Also add acceptance cases proving all of these existing contracts survive the
refactor: `pending_review` remains unclaimable until `batch_approved`; an
expired claim projects and lists as `pending`; a current-iteration
`codex_session` controls holdout release; `batch_done:succeeded` is rejected
without an earlier `validation_run`; and `failed_after_max_iterations` plus
`aborted_infra` are terminal. P07B proves evidence ordering only; D04 owns the
later semantic proof that a validation payload represents all holdouts green.

- [ ] **Step 2: Verify the acceptance tests fail**

Run: `python -m pytest -q tests/gateway/test_gateway.py -k 'append_events or lifecycle_update_sql'`

Expected: FAIL because claims, heartbeats, terminal status, and `children` currently mutate the parent block.

- [ ] **Step 3: Define immutable contracts and projection**

Create `gateway/contracts.py` with the following public shapes:

```python
class BatchProjection(BaseModel):
    batch_id: str
    parent_index: int
    status: Literal[
        "pending_review", "pending", "claimed", "succeeded", "failed",
        "rejected", "cancelled", "failed_after_max_iterations", "aborted_infra",
    ]
    claim_token_sha256: str | None = None
    claim_expires_at: datetime | None = None
    claim_iteration: int = 0
    codex_session_recorded: bool = False
    validation_run_recorded: bool = False


class ClaimEvent(BaseModel):
    batch_id: str
    claim_token_sha256: str
    claim_expires_at: datetime


class HeartbeatEvent(BaseModel):
    batch_id: str
    claim_expires_at: datetime


class EvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_id: str
    iteration: int = Field(ge=1)


class BatchDoneEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    batch_id: str
    outcome: Literal[
        "succeeded", "failed", "rejected", "cancelled",
        "failed_after_max_iterations", "aborted_infra",
    ]


def project_batch(
    blocks: Sequence[dict[str, Any]],
    batch_id: str,
    *,
    now: datetime | None = None,
) -> BatchProjection:
    """Derive the current state from a work_batch and ordered child events."""
```

`project_batch` starts from the immutable parent's `pending` or
`pending_review` status. `batch_approved` moves only `pending_review` to
`pending`. It counts claim events as fencing iterations, takes the newest
unexpired `batch_claimed` plus subsequent `batch_heartbeat`, and treats an
expired non-terminal lease as `pending`. `codex_session_recorded` and
`validation_run_recorded` describe only the current claim iteration. The first
valid `batch_done` maps to a terminal state; `succeeded` additionally requires
an earlier current-iteration `validation_run`. It raises `ValueError` for a
mismatched child `batch_id`, heartbeat or terminal event before a claim,
invalid approval ordering, or lifecycle events after the first terminal event.
The optional clock defaults to current UTC and exists only for deterministic
projection tests.

P07A verifies and commits only the pure boundary:

```powershell
python -m pytest -q --no-cov tests/gateway/test_contracts.py
git add gateway/contracts.py tests/gateway/test_contracts.py
git commit -m "feat: define gateway lifecycle projection"
```

- [ ] **Step 4: Extract `GatewayStore` and make it event-only**

Move `GatewayStore` and its MariaDB queries from `gateway/app.py` into
`gateway/store.py`; `gateway/app.py` keeps request routing, lifespan, mirror
enqueueing, and process composition. In the extracted store, replace parent
updates in `append`, `claim`, `heartbeat`, and `approve` with `_insert` calls
using these child block types: `batch_claimed`, `batch_heartbeat`,
`batch_approved`, and `batch_done`. Query children with
`WHERE parent_index=%s ORDER BY index`; never write the parent `status`,
`metadata`, or `children` fields after its initial insert. Make `/batches`,
`/batches/{batch_id}`, claim fencing, holdout access, and capability discovery
call `project_batch` through the store.

The token response remains:

```python
return {"claim_token": token, "claim_expires_at": expiry.isoformat()}
```

but the inserted event data contains only `ClaimEvent(..., claim_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest())`.

- [ ] **Step 5: Verify the MariaDB contract and commit**

Run: `python -m pytest -q tests/gateway/test_gateway.py tests/blockchain/test_mariadb_storage.py -rs`

Expected: all selected tests PASS with `TEST_MARIADB_DSN`; output contains no `SKIPPED`.

```powershell
git add gateway/store.py gateway/app.py tests/gateway/test_gateway.py tests/blockchain/test_mariadb_storage.py
git commit -m "refactor: persist gateway lifecycle as events"
```

## Task 3: Connect Captain and delivery clients exclusively through gateway HTTP

**Files:**
- Create: `agenten/planning/gateway_client.py`
- Create: `agenten/delivery/gateway_client.py`
- Modify: `agenten/planning/factory.py`
- Modify: `agenten/planning/cli.py`
- Modify: `agenten/delivery/api.py`
- Modify: `agenten/delivery/service.py`
- Modify: `agenten/delivery/__init__.py`
- Create: `tests/planning/test_gateway_client.py`
- Create: `tests/delivery/test_gateway_delivery_client.py`

**Interfaces:**
- Produces: `GatewayPlanningClient.release(batch, holdouts) -> None`, `GatewayPlanningClient.find_match(target, capability_tags) -> str | None`, and `GatewayDeliveryClient` methods `claim`, `heartbeat`, `append_evidence`, `complete`, and `get_batch`.
- Consumes: gateway `/blocks`, `/batches/{batch_id}/claim`, `/batches/{batch_id}/claim/heartbeat`, `/batches/{batch_id}`, and `/capabilities` routes.

- [ ] **Step 1: Write failing ASGI/HTTP client tests**

```python
@pytest.mark.asyncio
async def test_planning_client_releases_visible_batch_and_hidden_holdout(http: httpx.AsyncClient) -> None:
    client = GatewayPlanningClient("http://gateway", http, captain_token="captain-test")
    await client.release(batch_fixture(), holdout_fixture())
    assert (await http.get("/batches/batch-1/bundle")).status_code == 200
    assert (await http.get("/batches/batch-1/holdout")).status_code == 409


@pytest.mark.asyncio
async def test_delivery_client_sends_claim_token_only_on_fenced_requests(http: httpx.AsyncClient) -> None:
    client = GatewayDeliveryClient("http://gateway", http, worker_token="worker-test")
    claim = await client.claim("batch-1")
    await client.append_evidence("batch-1", claim.claim_token, evidence_fixture())
    captured = await last_request_headers(http)
    assert captured["x-claim-token"] == claim.claim_token
    assert "captain-test" not in captured.values()
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest -q tests/planning/test_gateway_client.py tests/delivery/test_gateway_delivery_client.py`

Expected: collection failure because the two gateway client modules do not exist.

- [ ] **Step 3: Implement typed, idempotent HTTP clients**

`GatewayPlanningClient.release` posts the immutable `work_batch`, then its `holdout` with the returned parent index. On HTTP 409 it fetches `/batches/{batch_id}/blocks`, compares canonical Pydantic JSON, and succeeds only if both stored objects match exactly; otherwise it raises `ReleaseConflictError`.

Use this constructor pattern in both clients:

```python
class GatewayPlanningClient(BatchReleaseClient, CapabilityResolver):
    def __init__(self, base_url: str, http: httpx.AsyncClient, *, captain_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._captain_token = captain_token
```

The delivery client emits worker lifecycle blocks, never SQL. Replace the production `create_delivery_app(SqliteDeliveryLedger(...))` composition with a proxy that holds a `GatewayDeliveryClient`; retain the SQLite class only for the migration script until Task 4 completes.

- [ ] **Step 4: Add explicit composition modes**

Extend Captain CLI options with `--release-mode {json,gateway}`, `--gateway-url`, and environment-backed `CAPTAIN_GATEWAY_TOKEN`. Keep `json` the default used by `python main.py demo`. Reject `--release-mode gateway` without `CAPTAIN_GATEWAY_TOKEN` before creating an HTTP client.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest -q tests/planning/test_gateway_client.py tests/delivery/test_gateway_delivery_client.py tests/planning/test_captain_pipeline.py`

Expected: PASS without a live MariaDB dependency because the new client tests use `httpx.ASGITransport`.

```powershell
git add agenten/planning/gateway_client.py agenten/delivery/gateway_client.py agenten/planning/factory.py agenten/planning/cli.py agenten/delivery/api.py agenten/delivery/service.py agenten/delivery/__init__.py tests/planning/test_gateway_client.py tests/delivery/test_gateway_delivery_client.py
git commit -m "feat: route captain delivery through gateway clients"
```

## Task 4: Import legacy SQLite state once and retire it from production paths

**Files:**
- Create: `scripts/migrate_sqlite_delivery_ledger.py`
- Modify: `agenten/delivery/ledger.py`
- Modify: `agenten/delivery/api.py`
- Modify: `tests/delivery/test_delivery_ledger.py`
- Create: `tests/delivery/test_sqlite_delivery_migration.py`

**Interfaces:**
- Produces: `python scripts/migrate_sqlite_delivery_ledger.py --sqlite-path <path> --gateway-url <url> --dry-run` and a JSON migration report.
- Consumes: legacy `delivery_todos` and `delivery_events` records; creates idempotent immutable gateway events keyed by legacy `event_id`.

- [ ] **Step 1: Write failing dry-run and replay tests**

```python
def test_migration_dry_run_writes_nothing(tmp_path: Path, gateway_client: RecordingGatewayClient) -> None:
    sqlite_path = seeded_legacy_ledger(tmp_path)
    report = migrate(sqlite_path, gateway_client, dry_run=True)
    assert report.imported_events == 0
    assert gateway_client.calls == []


def test_migration_replay_is_idempotent(tmp_path: Path, gateway_client: RecordingGatewayClient) -> None:
    sqlite_path = seeded_legacy_ledger(tmp_path)
    first = migrate(sqlite_path, gateway_client, dry_run=False)
    second = migrate(sqlite_path, gateway_client, dry_run=False)
    assert first.imported_events == 4
    assert second.already_present_events == 4
```

- [ ] **Step 2: Verify the migration tests fail**

Run: `python -m pytest -q tests/delivery/test_sqlite_delivery_migration.py`

Expected: FAIL because the migration module and `migrate` function do not exist.

- [ ] **Step 3: Implement a read-only, idempotent importer**

Open SQLite with `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. Read events ordered by `sequence`; map each legacy TODO to one deterministic `work_batch` ID `legacy-<todo_id>` and map each legacy event to a gateway child block containing:

```python
{
    "batch_id": f"legacy-{todo_id}",
    "legacy_event_id": event_id,
    "actor": actor,
    "event_type": event_type,
    "payload": json.loads(payload),
    "created_at": created_at,
}
```

`--dry-run` emits the report and sends no request. A non-dry run requires `--confirm-import`; every request is idempotent on `legacy_event_id`. The script must never delete, alter, or vacuum the SQLite file.

- [ ] **Step 4: Remove SQLite from the production API**

Delete the `SqliteDeliveryLedger` import from `agenten/delivery/api.py` and reject construction with it using:

```python
raise RuntimeError("SQLite delivery ledger is legacy-import only; use GatewayDeliveryClient")
```

Keep the class definition only while tests and the migration script need it. Move its direct tests under a `legacy` marker and remove it from the default documented execution path.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest -q tests/delivery/test_sqlite_delivery_migration.py tests/delivery/test_delivery_ledger.py`

Expected: PASS; legacy tests carry `@pytest.mark.legacy` and normal delivery clients do not import SQLite.

```powershell
git add scripts/migrate_sqlite_delivery_ledger.py agenten/delivery/ledger.py agenten/delivery/api.py tests/delivery/test_delivery_ledger.py tests/delivery/test_sqlite_delivery_migration.py
git commit -m "refactor: retire sqlite delivery control plane"
```

## Task 5: Secure and operate the sole-writer gateway

**Files:**
- Create: `gateway/auth.py`
- Create: `gateway/settings.py`
- Modify: `gateway/app.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `scripts/start_delivery_stack.ps1`
- Create: `tests/gateway/test_gateway_auth.py`
- Create: `tests/gateway/test_gateway_settings.py`

**Interfaces:**
- Produces: `GatewaySettings`, `require_captain`, `require_worker`, and a compose service named `gateway` listening only on `127.0.0.1:${GATEWAY_PORT:-8090}`.
- Consumes: `LEDGER_DSN`, `CAPTAIN_GATEWAY_TOKEN`, `WORKER_GATEWAY_TOKEN`, `GATEWAY_APPROVAL_ENABLED`, and `GATEWAY_PORT` from gitignored environment configuration.

- [ ] **Step 1: Write failing authorization and settings tests**

```python
def test_captain_write_requires_captain_bearer_token(client: TestClient) -> None:
    payload = {"block_type": "work_batch", "data": batch_payload()}
    assert client.post("/blocks", json=payload).status_code == 401
    assert client.post("/blocks", json=payload, headers={"Authorization": "Bearer captain-test"}).status_code == 201


def test_worker_cannot_create_captain_batch(client: TestClient) -> None:
    response = client.post("/blocks", json={"block_type": "work_batch", "data": batch_payload()}, headers={"Authorization": "Bearer worker-test"})
    assert response.status_code == 403
```

- [ ] **Step 2: Verify the tests fail**

Run: `python -m pytest -q tests/gateway/test_gateway_auth.py tests/gateway/test_gateway_settings.py`

Expected: FAIL because gateway routes do not authenticate actors and configuration is read ad hoc from `os.getenv`.

- [ ] **Step 3: Implement fail-closed settings and route dependencies**

Use a frozen settings model:

```python
class GatewaySettings(BaseSettings):
    ledger_dsn: str
    captain_gateway_token: SecretStr
    worker_gateway_token: SecretStr
    approval_enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8090
```

`require_captain` permits `problem`, `work_batch`, `holdout`, and approval writes. `require_worker` permits claim, heartbeat, evidence, Codex-session, and batch-done operations. Sink routes require the worker role; read routes require either role. Compare token values with `secrets.compare_digest` and do not log headers.

- [ ] **Step 4: Add the local service contract**

Add `gateway` to `docker-compose.yml` with `depends_on: mariadb: condition: service_healthy`, bind port `127.0.0.1:${GATEWAY_PORT:-8090}:8090`, and inject only `LEDGER_DSN`, approval flag, and token names from `.env`. Update `.env.example` with blank token placeholders and a DSN using the compose host `mariadb`, never a real credential. Make `scripts/start_delivery_stack.ps1` validate required variables, run `docker compose config`, start MariaDB and gateway, and wait for an authenticated `/healthz` endpoint without starting n8n.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest -q tests/gateway/test_gateway_auth.py tests/gateway/test_gateway_settings.py tests/gateway/test_gateway.py`

Run: `docker compose config`

Expected: tests PASS; compose configuration renders without warnings.

```powershell
git add gateway/auth.py gateway/settings.py gateway/app.py docker-compose.yml .env.example scripts/start_delivery_stack.ps1 tests/gateway/test_gateway_auth.py tests/gateway/test_gateway_settings.py
git commit -m "feat: secure and operate ledger gateway"
```

## Task 6: Make MariaDB proof, documentation, and release claims mandatory

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_ci_contract.py`
- Modify: `pytest.ini`
- Modify: `scripts/verify_submission.py`
- Rewrite: `docs/ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `docs/DEMO.md`
- Modify: `docs/DEVPOST_CHECKLIST.md`
- Modify: `docs/WORKSTREAMS.md`

**Interfaces:**
- Produces: a `mariadb-gateway` CI job with zero skips and public documentation that distinguishes offline evidence from live gateway evidence.
- Consumes: a MariaDB 11.8 service, `TEST_MARIADB_DSN`, the gateway auth contract, and the verified demo artifact.

- [ ] **Step 1: Write failing CI and documentation assertions**

```python
def test_ci_gateway_job_uses_mariadb_and_rejects_skips() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["mariadb-gateway"]
    assert "mariadb" in job["services"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "TEST_MARIADB_DSN" in commands
    assert "SKIPPED" in commands


def test_readme_does_not_claim_offline_demo_proves_live_gateway() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "The offline demo does not start or verify the MariaDB gateway." in readme
```

- [ ] **Step 2: Verify the assertions fail**

Run: `python -m pytest -q tests/test_ci_contract.py`

Expected: FAIL because no CI workflow enforces a real gateway/MariaDB gate.

- [ ] **Step 3: Implement the non-skipping gateway gate**

Create `mariadb-gateway` in `.github/workflows/ci.yml` using MariaDB 11.8. Set `TEST_MARIADB_DSN`, run `python -m pytest -q -rs tests/blockchain/test_mariadb_storage.py tests/gateway`, capture output, and fail when it contains `SKIPPED`:

```bash
python -m pytest -q -rs tests/blockchain/test_mariadb_storage.py tests/gateway | tee gateway-results.txt
! grep -q "SKIPPED" gateway-results.txt
```

Keep the ordinary unit job separate; it may omit this selected live database suite but may not claim it passed. Add `legacy` to `pytest.ini` and ensure `python scripts/verify_submission.py` reports the offline demo as offline evidence rather than live gateway proof.

- [ ] **Step 4: Align docs and verifier**

Rewrite the architecture flow as `Captain planning → Gateway HTTP → MariaDB append-only blocks → derived batch projection → worker claims/events → Minibook mirror`. State that `agenten/delivery/ledger.py` is legacy import support only. Update README/DEMO/Devpost text to say the deterministic demo is verified offline and include a separate gateway command requiring local MariaDB credentials. Add `LICENSE` acquisition to the Devpost owner-action list; do not invent or commit a license without an owner decision.

- [ ] **Step 5: Run the complete final gate and commit**

Run: `python -m pytest -q`

Run: `python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py tests/test_ci_contract.py`

Run: `python -m compileall -q agenten blockchain chats config gateway`

Run: `python scripts/verify_submission.py`

Run with a live local MariaDB: `python -m pytest -q -rs tests/blockchain/test_mariadb_storage.py tests/gateway`

Expected: all commands PASS; the selected MariaDB command reports zero skips.

```powershell
git add .github/workflows/ci.yml pytest.ini tests/test_ci_contract.py scripts/verify_submission.py docs/ARCHITECTURE.md README.md docs/DEMO.md docs/DEVPOST_CHECKLIST.md docs/WORKSTREAMS.md
git commit -m "docs: verify gateway delivery architecture"
```

## Merge Order and Stop Gates

1. `feat/gateway-append-only-contract` implements Task 2 after Task 1's boundary commit is merged.
2. `feat/gateway-clients` implements Task 3 after the Task 2 gateway routes and projections exist.
3. `feat/gateway-sqlite-retirement` implements Task 4 after Task 3; it is the only branch allowed to edit both legacy SQLite delivery files and the importer.
4. `feat/gateway-security-operations` implements Task 5 after Task 2; it may run in parallel with Task 3 but must rebase before merge.
5. `feat/gateway-live-evidence` implements Task 6 only after Tasks 2–5 have merged to the current `main`.

Stop and report rather than weakening the contract when: a branch needs direct MariaDB access outside `gateway/`; the event projection cannot represent an existing lifecycle transition; a migration record conflicts with different immutable content; a gateway/MariaDB test skips; holdout data appears in a non-authorized response; or credentials are missing for a requested live proof.

## Plan Self-Review

- [x] One production source of truth is enforced by the architecture test, HTTP clients, SQLite retirement, and documentation tasks.
- [x] Parent lifecycle mutation is replaced by immutable event blocks and a derived projection.
- [x] Offline demo behavior remains explicit and unchanged as a JSON adapter.
- [x] MariaDB proof, authentication, configuration, migration safety, release claims, and the known documentation gap each have a testable task.
- [x] File paths, public interfaces, verification commands, and Conventional Commit messages are specified for every task.

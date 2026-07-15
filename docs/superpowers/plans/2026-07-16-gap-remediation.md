# Captain Cook Gap Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the security, durability, live-evidence, recovery, Codex/Second-Brain, AutoGen, CI, and documentation gaps that prevent Captain Cook from operating as a crash-resumable real agent delivery team.

**Architecture:** Keep the existing deterministic Householder pipeline as the offline fallback. Promote the SQLite delivery ledger into the authoritative Captain command log with a transactional outbox; connect it to authenticated HTTP composition, Hermes/Minibook projections, supervised Codex processes, durable reasoning slices, and a fail-closed Quality Warden. MariaDB remains the separately owned sole-writer gateway for production lifecycle records and must be proven with a real container gate.

**Tech Stack:** Python 3.11, FastAPI, Pydantic 2, SQLite WAL, MariaDB, AutoGen Core 0.7.5, Hermes Agent, Codex CLI, Second Brain MCP, Minibook, pytest, PowerShell, GitHub Actions

## Global Constraints

- Never commit or print `.env`, Minibook/Hermes credentials, MCP tokens, database passwords, or raw prompts containing secrets.
- No mock, fake, stub, simulated tool, synthetic transcript, or manually asserted hash may satisfy a live acceptance gate.
- Unit tests may use deterministic in-process ports, but every external-system claim requires a separately marked fail-closed live test.
- Captain owns assignments and terminal decisions; Builder and Tester may not approve their own output.
- State and outbox rows commit atomically before notifications are delivered.
- Duplicate command and delivery IDs are idempotent under concurrency.
- A rejected fifth review becomes `escalated`; no sixth build begins.
- The deterministic offline demo remains runnable without Docker, LLM, MCP, Hermes, Minibook, or MariaDB.
- Each task starts with a focused failing test, runs the focused gate, then runs `python -m pytest -q` before commit.
- Do not rewrite `artifacts/demo-run.json` unless the task intentionally regenerates and verifies demo evidence.

---

### Task 1: Restore Minibook Admin Authentication

**Files:**
- Modify: `minibook/src/main.py:118-129`
- Modify: `minibook/config.example.yaml`
- Test: `minibook/tests/test_admin_auth.py`

**Interfaces:**
- Consumes: `Authorization: Bearer <admin_token>` and configured `ADMIN_TOKEN`
- Produces: `require_admin(authorization: str | None) -> bool`

- [ ] **Step 1: Write failing authentication tests**

```python
def test_admin_endpoint_rejects_missing_token(client):
    response = client.get("/api/v1/admin/projects")
    assert response.status_code == 401


def test_admin_endpoint_rejects_wrong_token(client):
    response = client.get(
        "/api/v1/admin/projects",
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 403


def test_admin_endpoint_accepts_configured_token(client, admin_headers):
    assert client.get("/api/v1/admin/projects", headers=admin_headers).status_code == 200
```

- [ ] **Step 2: Run the focused test and prove the bypass**

Run: `python -m pytest minibook/tests/test_admin_auth.py -v`

Expected: FAIL because missing and incorrect credentials currently return `200`.

- [ ] **Step 3: Implement fail-closed authentication**

```python
def require_admin(authorization: str | None = Header(None)) -> bool:
    if not ADMIN_TOKEN:
        raise HTTPException(503, "Admin authentication is not configured")
    if not authorization:
        raise HTTPException(401, "Admin token required")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(403, "Invalid admin token")
    return True
```

Document `admin_token: ${MINIBOOK_ADMIN_TOKEN}` without adding a real value.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest minibook/tests/test_admin_auth.py minibook/tests -q`

Run: `python -m pytest -q`

Commit: `fix: enforce minibook admin authentication`

---

### Task 2: Add a Transactional Delivery Outbox

**Files:**
- Modify: `agenten/delivery/models.py`
- Modify: `agenten/delivery/ledger.py`
- Create: `agenten/delivery/outbox.py`
- Modify: `agenten/delivery/api.py`
- Test: `tests/delivery/test_delivery_outbox.py`

**Interfaces:**
- Produces: `OutboxMessage`, `claim_outbox()`, `ack_outbox()`, `release_outbox()`, `DeliveryOutboxWorker.run_once()`
- Consumes: the existing SQLite transaction and `DeliveryEventPublisher`

- [ ] **Step 1: Write failing atomicity and concurrency tests**

```python
def test_transition_commits_event_and_outbox_atomically(real_ledger, assigned_todo):
    changed = real_ledger.transition(
        assigned_todo.todo_id,
        event_id="start-atomic",
        expected_version=assigned_todo.version,
        actor="architect_builder",
        target=DeliveryStatus.IN_PROGRESS,
    )
    pending = real_ledger.claim_outbox(worker_id="publisher-1", limit=10)
    assert [(item.event_id, item.todo_version) for item in pending] == [
        ("start-atomic", changed.version)
    ]


def test_two_workers_cannot_claim_the_same_outbox_message(real_ledger):
    first = real_ledger.claim_outbox(worker_id="one", limit=1)
    second = real_ledger.claim_outbox(worker_id="two", limit=1)
    assert {item.message_id for item in first}.isdisjoint(
        {item.message_id for item in second}
    )
```

- [ ] **Step 2: Run tests and verify missing outbox API**

Run: `python -m pytest tests/delivery/test_delivery_outbox.py -v`

Expected: FAIL because `delivery_outbox` and claim operations do not exist.

- [ ] **Step 3: Implement the outbox in the same SQLite transaction**

Add a `delivery_outbox` table with `message_id`, unique `event_id`, payload JSON, `available_at`, `lease_owner`, `lease_expires_at`, `attempts`, and `delivered_at`. Every accepted create, transition, heartbeat, and evidence command inserts exactly one outbox row before commit.

```python
class OutboxMessage(BaseModel):
    model_config = ConfigDict(frozen=True)
    message_id: str
    event_id: str
    todo_id: str
    todo_version: int
    payload: dict[str, object]
    attempts: int
```

Use `BEGIN IMMEDIATE` plus lease predicates for claims. Remove `has_event()` followed by publish from request handlers; API responses return committed state only.

- [ ] **Step 4: Implement retryable delivery**

```python
class DeliveryOutboxWorker:
    async def run_once(self) -> int:
        claimed = self.ledger.claim_outbox(self.worker_id, limit=self.batch_size)
        delivered = 0
        for message in claimed:
            try:
                await self.publisher.publish(message.to_event())
            except Exception:
                self.ledger.release_outbox(message.message_id, self.worker_id)
                continue
            self.ledger.ack_outbox(message.message_id, self.worker_id)
            delivered += 1
        return delivered
```

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest tests/delivery/test_delivery_outbox.py tests/delivery -q`

Run: `python -m pytest -q`

Commit: `feat: add transactional delivery outbox`

---

### Task 3: Compose and Authenticate the Captain Delivery Service

**Files:**
- Replace: `agenten/delivery/service.py`
- Create: `agenten/delivery/settings.py`
- Create: `agenten/delivery/auth.py`
- Create: `scripts/run_delivery_control_plane.py`
- Modify: `agenten/delivery/api.py`
- Test: `tests/delivery/test_delivery_service.py`

**Interfaces:**
- Produces: `DeliverySettings`, `build_delivery_service(settings) -> FastAPI`
- Consumes: SQLite path, Captain bearer-token environment variable, outbox poll interval

- [ ] **Step 1: Write failing composition and authorization tests**

```python
def test_control_plane_requires_captain_token(real_settings):
    client = TestClient(build_delivery_service(real_settings))
    assert client.get("/delivery/todos").status_code == 401


def test_service_reopens_existing_ledger(real_settings, captain_headers):
    first = TestClient(build_delivery_service(real_settings))
    created = first.post(
        "/delivery/todos",
        headers=captain_headers,
        json={
            "project_id": "captain-cook",
            "title": "Persist restart state",
            "description": "Reopen the same SQLite ledger",
            "acceptance_criteria": ["TODO survives service restart"],
        },
    ).json()
    second = TestClient(build_delivery_service(real_settings))
    loaded = second.get(
        f"/delivery/todos/{created['todo_id']}", headers=captain_headers
    )
    assert loaded.status_code == 200
```

- [ ] **Step 2: Prove the service and auth boundary are absent**

Run: `python -m pytest tests/delivery/test_delivery_service.py -v`

Expected: FAIL because `DeliverySettings` and `build_delivery_service` do not exist.

- [ ] **Step 3: Implement settings, auth dependency, lifecycle, and CLI**

```python
class DeliverySettings(BaseModel):
    ledger_path: Path
    captain_token: SecretStr
    host: str = "127.0.0.1"
    port: int = 8091
    outbox_poll_seconds: float = 1.0
```

`build_delivery_service()` owns one ledger, one outbox worker, startup/shutdown tasks, and token validation using `secrets.compare_digest`. The CLI reads environment variables through `pydantic-settings`, never command-line secrets.

- [ ] **Step 4: Verify and commit**

Run: `python -m pytest tests/delivery/test_delivery_service.py tests/delivery -q`

Run: `python -m pytest -q`

Commit: `feat: compose authenticated delivery control plane`

---

### Task 4: Make MariaDB and Integration Gates Mandatory in CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `scripts/verify_branch_edges.py`
- Modify: `pytest.ini`
- Modify: `docs/WORKSTREAMS.md`
- Test: `tests/test_ci_contract.py`

**Interfaces:**
- Produces: unit, architecture, MariaDB gateway, merge-edge, and submission jobs
- Consumes: MariaDB 11 service container and `TEST_MARIADB_DSN`

- [ ] **Step 1: Write a failing CI manifest contract**

```python
def test_ci_runs_real_mariadb_gateway_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["mariadb-gateway"]
    assert "mariadb" in job["services"]
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "tests/gateway" in commands
    assert "TEST_MARIADB_DSN" in str(job)
```

- [ ] **Step 2: Run and verify the missing workflow**

Run: `python -m pytest tests/test_ci_contract.py -v`

Expected: FAIL because `.github/workflows/ci.yml` does not exist.

- [ ] **Step 3: Implement CI jobs**

Define jobs for:

1. `unit`: `python -m pytest -q` excluding only `mariadb` and explicitly external live markers.
2. `architecture`: import fitness, compileall, workstream-doc tests.
3. `mariadb-gateway`: real MariaDB service, migrations, `tests/blockchain/test_mariadb_storage.py tests/gateway` with zero skips.
4. `merge-edges`: `python scripts/verify_branch_edges.py` over dependency edges declared in `docs/WORKSTREAMS.md`.
5. `submission`: `python scripts/verify_submission.py`.

Make a selected gate fail when it reports any skip.

- [ ] **Step 4: Verify CI schema and local non-container gates**

Run: `python -m pytest tests/test_ci_contract.py tests/test_workstream_docs.py -v`

Run: `python scripts/verify_branch_edges.py --base main`

Run: `python -m pytest -q`

Commit: `ci: require architecture and mariadb delivery gates`

---

### Task 5: Add the Supervised Codex CLI and Second Brain Adapter

**Files:**
- Create: `agenten/tools/codex_cli.py`
- Create: `agenten/delivery/codex_runs.py`
- Create: `scripts/codex-session.ps1`
- Create: `tests/tools/test_codex_cli_contract.py`
- Create: `tests/live/test_codex_secondbrain_live.py`
- Modify: `docs/codex-sessions.md`

**Interfaces:**
- Produces: `CodexRun`, `CodexRunStatus`, `CodexCli.start()`, `resume()`, `status()`, `cancel()`
- Consumes: authorized worktree, delivery TODO, immutable acceptance criteria, optional failure report

- [ ] **Step 1: Capture a real benign Codex JSONL session**

Run `codex exec --json` in a disposable Git worktree with the instruction to inspect `codex mcp list` and identify `secondbrain` without editing files. Store the sanitized JSONL fixture with usernames and absolute paths replaced, but preserve event types, session ID shape, and MCP discovery events.

- [ ] **Step 2: Write failing parser and workspace-guard tests**

```python
def test_real_jsonl_extracts_session_and_secondbrain(real_codex_jsonl):
    parsed = parse_codex_jsonl(real_codex_jsonl)
    assert parsed.session_id
    assert "secondbrain" in parsed.discovered_mcp_servers


def test_codex_rejects_workspace_outside_authorized_root(tmp_path):
    with pytest.raises(CodexWorkspaceError):
        CodexCli(authorized_root=tmp_path / "allowed").start(
            workspace=tmp_path / "outside", request=REQUEST
        )
```

- [ ] **Step 3: Implement argument-array process supervision**

Never concatenate a shell command. Persist PID, session ID, branch, before/after commit, timestamps, JSONL path, exit code, and heartbeat time in SQLite. Require a real session ID before success and redact environment values from captured output.

```python
process = subprocess.Popen(
    ["codex", "exec", "--json", "--cd", str(workspace), request.prompt],
    stdout=jsonl_handle,
    stderr=stderr_handle,
    text=True,
    env=restricted_environment(),
)
```

- [ ] **Step 4: Implement resume and cancellation**

Resume with `codex exec resume <session_id> --json`; reject resume when the TODO, workspace, or immutable acceptance hash differs. Cancel the exact recorded PID and persist the terminal outcome.

- [ ] **Step 5: Run the real Second Brain gate**

Run: `python -m pytest tests/live/test_codex_secondbrain_live.py -v -m live`

Expected: PASS only with an enabled real `secondbrain` MCP server, real Codex session ID, and recorded MCP discovery event. Missing service is FAIL, never SKIP.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest tests/tools/test_codex_cli_contract.py -v`

Run: `python -m pytest -q`

Commit: `feat: supervise codex cli delivery runs`

---

### Task 6: Persist Society-of-Mind Slices and Crash Recovery

**Files:**
- Create: `agenten/delivery/reasoning.py`
- Create: `agenten/delivery/recovery.py`
- Create: `agenten/delivery/scheduler.py`
- Create: `scripts/resume_delivery.ps1`
- Modify: `agenten/delivery/models.py`
- Modify: `agenten/delivery/ledger.py`
- Test: `tests/delivery/test_reasoning_recovery.py`
- Test: `tests/live/test_delivery_crash_recovery_live.py`

**Interfaces:**
- Produces: `ReasoningSlice`, `ReasoningStore`, `DeliveryRecovery`, `RecoveryDecision`
- Consumes: TODO version, Codex session ID, lease expiry, last completed reasoning turn

- [ ] **Step 1: Write failing restart tests for every non-terminal state**

```python
@pytest.mark.parametrize(
    "status, expected",
    [
        (DeliveryStatus.ASSIGNED, RecoveryDecision.REQUEUE),
        (DeliveryStatus.IN_PROGRESS, RecoveryDecision.RESUME_CODEX),
        (DeliveryStatus.TESTING, RecoveryDecision.REQUEUE_TESTER),
        (DeliveryStatus.REVIEWING, RecoveryDecision.REQUEUE_WARDEN),
        (DeliveryStatus.REDO, RecoveryDecision.REQUEUE_BUILDER),
    ],
)
def test_restart_recovers_non_terminal_state(status, expected, persisted_scenario):
    assert DeliveryRecovery(persisted_scenario.reopened_ledger).decide(
        persisted_scenario.todo_id
    ) is expected
```

- [ ] **Step 2: Write safe reasoning-boundary tests**

Prove each slice has a bounded turn count, immutable input hash, summarized public result, private transcript path, next owner, and wake condition. A restarted process continues from the committed slice rather than replaying the whole conversation.

- [ ] **Step 3: Implement durable reasoning slices**

Use three roles: `architect_builder`, `real_case_tester`, and `quality_warden`. Each AutoGen conversation runs for a bounded number of turns, commits a slice, exits, and is awakened by an outbox event. Do not keep one unbounded in-memory conversation alive.

- [ ] **Step 4: Implement lease reaping and Windows wake entry point**

The scheduler scans expired leases, stale Codex PIDs, and pending outbox rows. `resume_delivery.ps1` starts one idempotent recovery pass and returns nonzero when required dependencies are unavailable.

- [ ] **Step 5: Execute a real crash/restart test**

Start a real delivery case, terminate the Captain process after a committed reasoning slice, restart it, and require the same TODO ID, iteration, Codex session ID, and next-owner decision. No synthetic state injection may satisfy this test.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest tests/delivery/test_reasoning_recovery.py -v`

Run: `python -m pytest tests/live/test_delivery_crash_recovery_live.py -v -m live`

Run: `python -m pytest -q`

Commit: `feat: resume durable reasoning after restart`

---

### Task 7: Enforce Real Evidence and the Five-Iteration Quality Gate

**Files:**
- Create: `agenten/delivery/evidence_policy.py`
- Create: `agenten/delivery/controller.py`
- Modify: `agenten/delivery/models.py`
- Modify: `agenten/delivery/ledger.py`
- Test: `tests/delivery/test_evidence_policy.py`
- Test: `tests/live/test_real_case_iteration_live.py`

**Interfaces:**
- Produces: `EvidenceVerdict`, `EvidencePolicy.verify()`, `DeliveryController.advance()`
- Consumes: immutable acceptance criteria, command transcripts, commit IDs, test reports, HTTP/MCP correlation IDs

- [ ] **Step 1: Write fail-closed evidence tests**

```python
@pytest.mark.parametrize("forbidden", ["mock", "fake", "stub", "simulated"])
def test_live_gate_rejects_synthetic_evidence(forbidden, evidence_factory):
    evidence = evidence_factory(kind=forbidden)
    assert EvidencePolicy().verify(evidence).accepted is False


def test_gate_recomputes_artifact_hash(tmp_path, evidence_factory):
    artifact = tmp_path / "result.json"
    artifact.write_text("real", encoding="utf-8")
    evidence = evidence_factory(uri=str(artifact), sha256="0" * 64)
    assert EvidencePolicy().verify(evidence).reason == "sha256_mismatch"
```

- [ ] **Step 2: Implement typed evidence verification**

Require evidence-specific validators for Codex JSONL, Git commits, pytest JUnit, HTTP responses, Minibook posts, n8n executions, Mailpit messages, and MCP discovery. Validators reread the artifact or external system and correlate TODO/run IDs.

- [ ] **Step 3: Implement independent review and iteration control**

Builder submits evidence, Tester reproduces acceptance criteria, Quality Warden decides `passed` or `redo`. A `redo` increments exactly once; rejection at iteration five becomes `escalated` atomically.

- [ ] **Step 4: Execute a real red-to-green case**

Use a harmless real repository defect. Require at least one failed test, Codex correction, green rerun, independent Tester reproduction, and Quality Warden approval.

- [ ] **Step 5: Execute the five-red escalation case**

Use a deliberately impossible acceptance criterion against a real command. Prove five recorded attempts, five failure reports, terminal `escalated`, and no sixth Codex process.

- [ ] **Step 6: Verify and commit**

Run: `python -m pytest tests/delivery/test_evidence_policy.py -v`

Run: `python -m pytest tests/live/test_real_case_iteration_live.py -v -m live`

Run: `python -m pytest -q`

Commit: `feat: enforce real-case quality evidence`

---

### Task 8: Connect Hermes Workers and Selective Learning

**Files:**
- Create: `agenten/delivery/hermes_team.py`
- Create: `agenten/delivery/learning.py`
- Create: `scripts/provision_hermes_team.py`
- Modify: `agenten/delivery/projector.py`
- Test: `tests/delivery/test_selective_learning.py`
- Test: `tests/live/test_hermes_team_delivery_live.py`

**Interfaces:**
- Produces: `HermesTeamProvisioner`, `LearningCandidate`, `LearningPromotionPolicy`
- Consumes: delivery TODO/outbox events, three fixed Hermes identities, validated failure/correction pairs

- [ ] **Step 1: Write failing projection and learning tests**

Prove each assigned TODO appears in the correct Hermes queue and Minibook post, duplicate delivery events do not duplicate tasks, successful noise creates no learning candidate, and only poor/error/nonsensical output plus a validated correction creates one.

- [ ] **Step 2: Implement idempotent worker provisioning**

Provision `architect_builder`, `real_case_tester`, and `quality_warden` with separate credentials stored only in the Hermes user profile. Persist external IDs in delivery projection metadata; never post full private transcripts.

- [ ] **Step 3: Implement two-proof promotion**

```python
def promotable(candidate: LearningCandidate) -> bool:
    return (
        candidate.validated_applications >= 2
        and candidate.quality_warden_approved
        and candidate.contains_secret is False
    )
```

Store detailed failure plus compact successful correction. Promote to a Hermes skill only after two independent validated applications and Quality Warden approval.

- [ ] **Step 4: Execute the complete live team case**

Captain posts a TODO, Builder runs real Codex/Second Brain, Tester executes the real case, Warden rejects or approves, Minibook mirrors each committed transition, and restart recovery completes without operator state repair.

- [ ] **Step 5: Verify and commit**

Run: `python -m pytest tests/delivery/test_selective_learning.py -v`

Run: `python -m pytest tests/live/test_hermes_team_delivery_live.py -v -m live`

Run: `python -m pytest -q`

Commit: `feat: connect hermes team selective learning`

---

### Task 9: Make the AutoGen Runtime Boundary Explicit and Supported

**Files:**
- Modify: `agenten/runtime/event_bus.py`
- Modify: `agenten/runtime/autogen_bus.py`
- Modify: `agenten/orchestration/pipeline.py`
- Modify: `agenten/runtime/bootstrap.py`
- Test: `tests/test_autogen_bus_integration.py`
- Test: `tests/test_pipeline_autogen_subscription.py`

**Interfaces:**
- Produces: separate `Publisher` and `CallableSubscriptionBus` capabilities, boot-time capability validation
- Consumes: AutoGen `TypeSubscription` registration for routed agents

- [ ] **Step 1: Write failing capability tests**

```python
def test_pipeline_rejects_publish_only_bus_before_constructing_agents():
    with pytest.raises(EventBusCapabilityError, match="callable subscriptions"):
        build_pipeline(bus=PublishOnlyBus(), config=CONFIG)
```

Add a real AutoGen test that registers routed agent types and round-trips one event without calling `AutoGenEventBus.subscribe()`.

- [ ] **Step 2: Split the incompatible protocol**

```python
class EventPublisher(Protocol):
    async def publish(self, topic: str, event: object) -> None: ...


class CallableSubscriptionBus(EventPublisher, Protocol):
    def subscribe(self, topic: str, handler: Handler) -> None: ...
```

`InMemoryEventBus` implements both. `AutoGenEventBus` implements publishing only; AutoGen routed-agent registration is composed separately. Remove the misleading `subscribe()` implementation that raises `NotImplementedError`.

- [ ] **Step 3: Verify and commit**

Run: `python -m pytest tests/test_autogen_bus_integration.py tests/test_pipeline_autogen_subscription.py -v`

Run: `python -m pytest -q`

Commit: `refactor: make autogen bus capabilities explicit`

---

### Task 10: Align Architecture, Claims, Packaging Backlog, and Release Verification

**Files:**
- Rewrite: `docs/ARCHITECTURE.md`
- Rewrite: `docs/WORKSTREAMS.md`
- Modify: `README.md`
- Modify: `docs/DEMO.md`
- Modify: `docs/DEVPOST_CHECKLIST.md`
- Modify: `scripts/verify_submission.py`
- Modify: `docs/superpowers/plans/2026-07-15-architecture-gap-todos.md`
- Create: `tests/test_claim_evidence_contract.py`

**Interfaces:**
- Produces: current architecture map, canonical `main` baseline, evidence-backed claim manifest
- Consumes: committed live evidence manifests from Tasks 4–9

- [ ] **Step 1: Write a failing claim-evidence contract**

```python
def test_every_public_live_claim_has_verified_manifest():
    claims = load_public_claims(ROOT / "docs" / "claims.yaml")
    for claim in claims:
        if claim.status == "live":
            assert claim.evidence_manifest
            assert verify_manifest(ROOT / claim.evidence_manifest) == []
```

- [ ] **Step 2: Rewrite architecture around the real path**

Document events → decomposition → constitution → routing → workers → supervision → recorder/query, plus the separate delivery ledger/outbox, MariaDB gateway, Hermes/Minibook projection, Codex/Second-Brain processes, and recovery scheduler. Record allowed imports and deployment ownership for root runtime, Minibook, Hermes, and VibeMind n8n.

- [ ] **Step 3: Update workstreams and close only proven checkboxes**

Set `main` as canonical integration baseline. Retire merged branch aliases only after explicit approval. Keep packaging migration, `web_scamler.py` adapter movement, recorder/pipeline splits, and `chats/project_maker.py` removal as dated follow-up items unless implemented in a separately reviewed branch.

- [ ] **Step 4: Strengthen submission verification**

Verify artifact hashes, commit IDs, timestamps, tool versions, test reports, and correlation IDs—not merely file existence and JSON shape. Explicitly distinguish offline evidence from live delivery evidence.

- [ ] **Step 5: Run the final release gate**

Run: `python -m pytest -q`

Run: `python -m pytest -q tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py tests/test_claim_evidence_contract.py`

Run: `python -m compileall -q agenten blockchain chats config gateway`

Run: `python scripts/verify_submission.py`

Run the marked MariaDB, Codex/Second-Brain, crash-recovery, real-case, and Hermes live suites with zero skips. Run `python main.py demo --output <temporary-path>` and compare the verified output without overwriting `artifacts/demo-run.json` unless the change is intentional.

- [ ] **Step 6: Commit**

Commit: `docs: align architecture and evidence claims`

---

## Merge Order and Stop Gates

1. Tasks 1–4 may merge independently after their own gates.
2. Task 5 requires Tasks 2–4.
3. Task 6 requires Tasks 2, 3, and 5.
4. Task 7 requires Tasks 5 and 6.
5. Task 8 requires Tasks 2, 3, 6, and 7.
6. Task 9 is independent but must land before claiming a live AutoGen pipeline.
7. Task 10 runs only after the implementation tasks selected for the release are merged.

Stop immediately when a live dependency is unavailable, a selected live test skips, evidence cannot be independently reread, or a fifth review fails. Record the failure in the delivery ledger; do not convert it into a synthetic pass.

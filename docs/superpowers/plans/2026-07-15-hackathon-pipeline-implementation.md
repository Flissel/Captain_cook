# Captain → Hermes → Codex Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the OpenAI Build Week submission — a pipeline where the Captain decomposes a project into ledger work-batches, a Hermes worker fleet drives Codex to build them (n8n workflow-tools and autogen-core 0.7.5 orchestrators), and each is validated against holdout tests, all recorded in a MariaDB ledger and watchable in Minibook.

**Architecture:** Two layers — autogen-core 0.7.5 agent teams (brain) orchestrate n8n workflows exposed as tool calls (hands). Captain (GPT-5.6, standalone process) → MariaDB ledger via a FastAPI gateway (sole writer, fencing) → Hermes cron workers claim batches and drive `codex exec` → target adapter (n8n | autogen) deploy/execute/observe → holdout validation → `batch_done`. Minibook mirrors the ledger as a read-model + hosts the validated-capability registry.

**Tech Stack:** Python 3.11, autogen-core/agentchat/ext 0.7.5, FastAPI + uvicorn, MariaDB 11.8 (PyMySQL), Codex CLI, hermes-agent (NousResearch), n8n 2.29.x + Mailpit (Docker), Minibook (FastAPI + SQLite), pytest.

## Execution model (READ FIRST — differs from a standard plan)

This plan is executed under the hackathon's Codex-authorship rule. Therefore:

- **CORE code tasks** (gateway, storage, Captain stages, adapters, worker skill, validation harness) give a **build contract** (what to author, interfaces, constraints) + an **acceptance test** (how a reviewer/Hermes verifies) — the implementation is authored by **Codex** in the designated **primary thread** (spec §2), NOT pre-written here. Claude Code / a review subagent reviews the Codex output against the acceptance test.
- **SETUP tasks** (compliance, docker-compose, credentials, scripts, docs) have concrete content/commands — they are project setup, not "core functionality", and are fine to specify literally.
- Every Codex-authored task records its session ID via the capture wrapper (Task 1) and a `Codex-Session:` commit trailer. The primary thread is resumed (`codex exec resume`) across core tasks so one thread holds the majority of core functionality (the `/feedback` form field).
- Reference: design spec `docs/superpowers/specs/2026-07-15-hackathon-pipeline-design.md`. Every task cites the spec section it implements.

## Global Constraints

- Deadline: submit before **2026-07-21 17:00 PT**; Tue 21 is buffer, target Mon 20.
- All new CORE implementation authored in **Codex sessions**; Claude/Hermes plan & review. (§2)
- Model is **`gpt-5.6`** everywhere (`config/llm_config.py`, env-overridable). No `gpt-4o`. (§2)
- Framework for built agents: **autogen-agentchat/autogen-core 0.7.5 ONLY**. Banned in generated code: `pyautogen`, `config_list=`, `llm_config=`, `UserProxyAgent`, `register_nested_chats`, `initiate_chat`. (§21.2)
- Ledger = **MariaDB** (sole writer = gateway); agents reach it via gateway HTTP. Registry stays on Minibook SQLite. n8n keeps its own SQLite volume. (§22)
- **No secrets** in the ledger, Minibook, codex workspaces, or cron prompts; repo is already public. (§13)
- Public phrasing: "append-only audited ledger with per-block hashes" — never "tamper-proof blockchain". (§4)
- Target enum populated from config, not LLM choice: `"n8n"` | `"autogen"`. (§6, §21)
- Hermes memory + shared skills dir are NEVER wiped by the reset script (learning substrate). (§21.3, §22)

---

## Phase 1 — Foundation, compliance & Gate A (Day 1, Wed 15)

Deliverable: a green Gate-A spike (Codex builds a trivial n8n workflow → deployed → webhook → mail in Mailpit, twice) on a running docker stack, with all compliance artifacts committed and the primary Codex thread opened.

### Task 1: Compliance & session-capture foundation

**Files:**
- Create: `docs/codex-sessions.md`, `PRIOR_WORK.md`, `.gitignore` (append), `scripts/codex-session.sh` (wrapper)
- Create: `.gitmodules` (via `git submodule add`)
- Modify: `config/llm_config.py` (model → gpt-5.6)

- [ ] **Step 1: Write the Codex session-capture wrapper** — `scripts/codex-session.sh`: wraps `codex exec`, parses `thread_id` from the `thread.started` JSONL event, appends `<id> | <date> | <intent>` to `docs/codex-sessions.md`. Written BEFORE any `codex exec`. (§2)
- [ ] **Step 2: Baseline tag + PRIOR_WORK.md** — verify the official cutoff (2026-07-13 09:00 PT) in the rules; commit the current tree; `git tag pre-codex-baseline`; `git push origin pre-codex-baseline`. `PRIOR_WORK.md` documents the three bands (pre-13.07 / 13–14.07 Claude foundation / post-tag Codex work). (§2)
- [ ] **Step 3: Resolve the last embedded repo** — DONE for minibook (vendored at `./minibook`, PR #15; `Autogen_AgentFarm` gitlink dropped). REMAINING: `hermes-agent/` is a bare gitlink with no `.gitmodules` → decide vendor vs. proper submodule (`git submodule add https://github.com/NousResearch/hermes-agent.git hermes-agent`, pin 77d5b2d). (§2, §23)
- [ ] **Step 4: Clone smoke test** — `git clone --recursive` into a temp dir; confirm `minibook/` is populated and `hermes-agent/` resolves (once Step 3's hermes decision lands). The fix is not done until the clone proves it. (§2)
- [ ] **Step 5: `.gitignore` append** — add `workspaces/`, `runs/`, `worker.env`, `.env`, `*.local`. (§14)
- [ ] **Step 6: Model → gpt-5.6** — `config/llm_config.py` `MODEL = os.getenv("CAPTAIN_MODEL", "gpt-5.6")`. Authored in the primary Codex thread (touches prior-work file). Re-run decompose/judge smoke tests. (§2)
- [ ] **Step 7: Freeze the in-flight refactor** — add a FROZEN banner to `docs/superpowers/plans/2026-07-14-autogen-runtime-boundary-refactor.md`. (§2)
- [ ] **Step 8: Commit** — `git add … && git commit -m "chore: hackathon baseline, submodules, gpt-5.6, session capture"` with a `Codex-Session:` trailer for the model change.

### Task 2: Devpost registration & credits (external, do early)

- [ ] **Step 1:** Register the Devpost account, join the hackathon.
- [ ] **Step 2:** Request the $100 Codex credits on the Resources tab (before Fri 17.07 12:00 PT). (§2)
- [ ] **Step 3:** Create a DRAFT submission; copy every form field into spec §18 (note the mandatory `/feedback` Session-ID field). (§2, §18)
- [ ] **Step 4:** Confirm the ChatGPT-plan rate limits cover ~50–100 exec turns/week; else flip codex auth to the API key. (§16)

### Task 3: Docker stack + n8n bootstrap

**Files:**
- Create: `docker-compose.yml`, `.env.example`, `.env` (gitignored), `templates/AGENTS.md` (minimal, Gate-A)

- [ ] **Step 1: `docker-compose.yml`** — three services: `n8n` (pinned 2.29.x, own SQLite volume `n8n_data`, env: `N8N_INSTANCE_OWNER_MANAGED_BY_ENV`, owner email/name/password-hash, `N8N_MCP_MANAGED_BY_ENV=true`, `N8N_MCP_ACCESS_ENABLED=true`, port 5678), `mailpit` (ports 8025 UI / 1025 SMTP), `mariadb` (11.8, volume `ledger_data`, `MARIADB_DATABASE=ledger`, creds from `.env`, port 3306). (§10, §22)
- [ ] **Step 2: `.env.example` + `.env`** — fixed ports (gateway 8090, minibook 8080, n8n 5678, mailpit 8025/1025, mariadb 3306) and credential placeholders. Never commit `.env`. (§10, §13)
- [ ] **Step 3: Bring up + healthcheck** — `docker info` preflight; `docker compose up -d`; wait for n8n + Mailpit + MariaDB healthy. (§14)
- [ ] **Step 4: n8n one-time bootstrap** — create the n8n API key + copy the MCP access token from the UI; `setx N8N_MCP_TOKEN <token>` (user-level, for dev + workers). Pre-provision two n8n credentials: `mailpit-smtp` and `openai-gpt` (GPT-5.6). Persist the volume — never delete it. (§10)
- [ ] **Step 5: Minimal `templates/AGENTS.md`** — credential rules (`mailpit-smtp` only, never create SMTP) + webhook path `/hook/{batch_id}` — enough for the Gate-A trivial build. (§9)
- [ ] **Step 6: Register MCP servers on Codex** — `codex mcp add n8n --url http://localhost:5678/mcp-server/http --bearer-token-env-var N8N_MCP_TOKEN`; add Context7 for the autogen path. `default_tools_approval_mode="auto"`. (§9, MCP-provisioning assessment)
- [ ] **Step 7: Commit** — docker-compose + env template (NOT `.env`).

### Task 4: GATE A (binary, scripted) — the go/no-go

**Files:** Create `scripts/gate-a.sh`

- [ ] **Step 1: n8n build spike** — from a fresh `workspaces/gate-a/`, `codex exec` (via the wrapper) builds a trivial n8n workflow through the n8n MCP; assert the n8n tool names appear in the `--json` event stream. (§16)
- [ ] **Step 2: deploy + webhook + mail** — deploy the workflow (MCP publish or REST), POST a test payload to its webhook, assert the mail lands in Mailpit — **twice in a row**. (§16)
- [ ] **Step 3: REST-deploy curl** — exercise the REST deploy path once so the JSON fallback is proven, not assumed. (§16)
- [ ] **Step 4: autogen serialization spike** — dump a team with a `FunctionTool`; confirm WHERE tools serialize (`config.workbench`) and whether a `load_component` round-trip executes them; confirm a keyless `OpenAIChatCompletionClient` dumps `api_key: null`. **Freezes the autogen deploy-gate schema.** (§21.2)
- [ ] **Step 5: resume + /feedback** — verify `codex exec resume <thread_id>` and how `/feedback` is run against the primary thread. (§2)
- [ ] **GATE A DECISION (15:00):** MCP path green twice → primary build path = n8n MCP. Else → JSON+REST fallback. Record the shipped posture in the README notes. If Gate A red on both → escalate (the whole pipeline depends on headless codex-builds-n8n).
- [ ] **Step 6: Install one Hermes instance in parallel** (profile, config.yaml with `model.provider: custom` → gpt-5.6, `.env`). Manual now; scripted Day 3. (§8)

---

## Phase 2 — Ledger gateway, Captain, single-worker E2E & Gate B (Days 2–3, Thu–Fri 16–17)

Deliverable: one Hermes worker takes a Captain-produced n8n batch end-to-end to a green `batch_done`, recorded in MariaDB and mirrored to Minibook.

### Task 5: MariaDBStorage behind LedgerStorage

**Files:** Create `blockchain/mariadb_storage.py`, `tests/blockchain/test_mariadb_storage.py`; Modify `requirements.txt` (add `pymysql`, `fastapi`, `uvicorn`)

**Interfaces:**
- Consumes: existing `LedgerStorage` ABC (`load`/`save`/`clear`) in `blockchain/storage.py`.
- Produces: `MariaDBStorage(dsn)` implementing `LedgerStorage` + an incremental `append_block(block)`; `blocks` table (`index` PK, `parent_index` FK, `block_type`, `data` JSON, `metadata` JSON, `hash`, `previous_hash`, `created_at`).

- [ ] **Step 1 (Codex build contract):** author `MariaDBStorage` implementing `LedgerStorage`; transactional writes; `append_block` for incremental appends; a status projection query. Pin `pymysql`, `fastapi`, `uvicorn` in requirements.txt. (§22)
- [ ] **Step 2 (acceptance test):** `pytest tests/blockchain/test_mariadb_storage.py` — round-trips a chain; two concurrent appends both persist (no last-writer-wins); a malformed row does not wipe the chain. Reviewer verifies against a live MariaDB container.
- [ ] **Step 3: Hash stability fix** — exclude mutable fields (children/status) from `compute_hash` in `blockchain/Blockchain_modell.py` so hashes are stable from creation. (§4)
- [ ] **Step 4: Commit** (Codex-Session trailer).

### Task 6: LedgerClient seam in Captain

**Files:** Modify `agenten/Captain.py` (lines 6, 27, 32–36), `main.py`, `tests/workflows/test_base.py`; Create `agenten/ledger_client.py`

**Interfaces:**
- Produces: `LedgerClient` protocol (`add_block`, `get_blocks`); `GatewayHTTPClient(base_url)` (prod), `DirectBlockchainClient(storage)` (tests). `CaptainAgent(..., ledger_client=...)` — drops the eager `Blockchain()` construction and the `Blockchain` import at line 6.

- [ ] **Step 1 (Codex build contract):** author `LedgerClient` + both impls; inject into `CaptainAgent`; remove eager store construction. (§5)
- [ ] **Step 2 (acceptance test):** import-boundary test (pattern `tests/test_import_boundaries.py`) — only the gateway package imports `Blockchain_modell`/`MariaDBStorage`, with a whitelist for `agenten/ledger_bridge/`. `CaptainAgent` constructs with a fake client and never opens a store.
- [ ] **Step 3: Commit.**

### Task 7: Assertion vocabulary + rubric (FROZEN Day 2)

**Files:** Create `agenten/validation/assertions.py`, `tests/validation/test_assertions.py`

**Interfaces:**
- Produces: Pydantic enums — n8n subset (`mail_sent`, `no_mail`, `webhook_response`, `sink_called`, `execution_status`) and autogen subset (`final_output`, `tool_called`, `tool_not_called`, `termination_reason`, `speaker_participated`, `sink_called`); rubric-v1 version string; `ContextBundle`, `AutogenTeamConfig` schemas.

- [ ] **Step 1 (Codex build contract):** author the closed assertion enums (each bound 1:1 to an observation channel) + the batch/bundle/team schemas. This is the Captain↔adapter interface — freeze it. (§7, §21.2)
- [ ] **Step 2 (acceptance test):** unobservable assertions are rejected by construction; a `webhook_response` requires `case_id` + `route`. `pytest tests/validation/test_assertions.py`.
- [ ] **Step 3: Commit.**

### Task 8: Ledger-Gateway (FastAPI, sole writer)

**Files:** Create `gateway/app.py`, `gateway/mirror.py`, `gateway/registry_feed.py`, `tests/gateway/test_gateway.py`

**Interfaces:**
- Consumes: `MariaDBStorage`, assertion schemas.
- Produces endpoints (§5, §21.4): `GET /batches?status=pending` (ids only), `POST /batches/{id}/claim` (→ claim_token | 409), `POST /batches/{id}/claim/heartbeat`, `POST /batches/{id}/approve`, `POST /blocks` (worker blocks fenced by token; Captain pre-claim blocks unfenced), `GET /batches/{id}/bundle` (holdout excluded), `GET /batches/{id}/blocks` (holdout excluded), `GET /batches/{id}/holdout` (token-fenced, only after a `codex_session` exists), `POST /sink/crm`, `GET /sink/crm`, `GET /capabilities?need=`.

- [ ] **Step 1 (Codex build contract):** author the gateway. Sole writer over MariaDB; claim = atomic transactional compare-and-set (`SELECT … FOR UPDATE`), fencing tokens, lazy expiry (90 min initial, +30 min heartbeat), terminal-state rejects. All write handlers `async def`, uvicorn `workers=1`. Fire-and-forget Minibook mirror queue (`gateway/mirror.py`). Registry feed (`gateway/registry_feed.py`) mirrors `batch_done:succeeded` via `minibook/swarm/api_client.py::register_agent_in_registry` called with `registry_agent_api_key=None` (gates off forum coupling), `status='validated'` only. Mock-CRM sink. `GET /capabilities` = indexed query over validated batches. (§5, §21.4, §23)
- [ ] **Step 2 (acceptance test):** `pytest tests/gateway/test_gateway.py` — two workers cannot both claim one batch; a block without the current token 409s; holdout is 404 before `codex_session` exists and served after; a terminal batch rejects further blocks; mirror failure never fails a ledger write. Reviewer runs against live MariaDB + Minibook.
- [ ] **Step 3: Reset script** — `scripts/reset.sh` (§14): disable worker crons → stop workers → archive ledger + `workspaces/` to `runs/<ts>/` → delete all n8n workflows → wipe Mailpit → optionally minibook.db. **Never** touches Hermes memory / shared skills / n8n volume.
- [ ] **Step 4: Commit.**

### Task 9: Captain pipeline (align, enrich, driver)

**Files:** Create `agenten/llm/align.py`, `agenten/llm/align_judge.py`, `agenten/llm/enrich.py`, `agenten/pipeline/captain_pipeline.py`, `tests/pipeline/test_captain_pipeline.py`

**Interfaces:**
- Consumes: existing `agenten/llm/decompose.py` + `judge.py` patterns; `GatewayHTTPClient`; assertion schemas.
- Produces: `make_align_batches(model_client)` → `AlignResponse{batches:[{batch_id,title,subtask_ids}]}`; `make_enrich_batch(model_client)` → `ContextBundle`; `captain_pipeline.py` CLI (`python -m agenten.pipeline.captain_pipeline demo/project_description.md`).

- [ ] **Step 1 (Codex build contract):** author align/enrich as `agenten/llm/` factory functions (decompose.py pattern: fresh AssistantAgent, `output_content_type`, raise on non-structured). The driver runs project_definition → decompose → align (deterministic set-checks first, then LLM judge for buildability, max 2 rounds) → per-batch enrich (with `satisfied_by` reuse via `GET /capabilities`, §21.4) → release `work_batch` + `holdout_cases` via the gateway. Dependency edges wired (§21). `batch_id` slug `^[a-z0-9-]{1,32}$`; derived names in the constants module. Target from config (`"n8n"`). (§6, §21.4)
- [ ] **Step 2 (acceptance test):** feeding the demo description yields ≥1 batch with valid bundles; every subtask id lands in exactly one batch; a needed capability already in the ledger is referenced (`satisfied_by`), not rebuilt. `pytest tests/pipeline/test_captain_pipeline.py`.
- [ ] **Step 3: Commit.**

### Task 10: Templates + validation harness

**Files:** Create `templates/AGENTS.md` (full n8n contract), `templates/codex_task.md`, `templates/failure_report.md`, `scripts/validate.py`, `tests/validation/test_validate.py`

**Interfaces:**
- Produces: `validate.py` — given a batch + deployed artifact + holdout cases, runs correlation polling (case_id, backoff), Mailpit/sink observation, assertion eval + soft-check judge, and returns a `validation_run` payload with full evidence + infra/behavioral classification.

- [ ] **Step 1 (Codex build contract):** author the full n8n `AGENTS.md` (§9 contract: webhook `/hook/{batch_id}`, echo `{case_id, route}`, mail subject `[case:<id>]`, `mailpit-smtp`/`openai-gpt` by name, no invented rules), `codex_task.md`, `failure_report.md`, and `validate.py`. (§7, §9, §11)
- [ ] **Step 2 (acceptance test):** `validate.py` correctly correlates a case by `case_id`, distinguishes infra (ECONNREFUSED) from behavioral failure, and emits evidence. `pytest tests/validation/test_validate.py`.
- [ ] **Step 3: Commit.**

### Task 11: n8n target adapter

**Files:** Create `agenten/adapters/n8n_adapter.py`, `tests/adapters/test_n8n_adapter.py`

**Interfaces:**
- Produces: `deploy(artifact)` (per-path: MCP verify-and-adopt / REST upsert), `execute(test_case)` (POST to webhook), `observe()` (executions API + Mailpit + sink).

- [ ] **Step 1 (Codex build contract):** author the n8n adapter per §10 (strip read-only fields, `settings:{}`, separate activate, idempotent upsert; deterministic name `captain-batch-{batch_id}`). (§10, §11)
- [ ] **Step 2 (acceptance test):** deploying twice yields exactly one published workflow; `observe()` reads back a known execution + mail. Live n8n.
- [ ] **Step 3: Commit.**

### Task 12: captain-worker skill + provisioning (single instance)

**Files:** Create `workers/skills/captain-worker/SKILL.md`, `workers/skills/captain-worker/scripts/*.sh`, `scripts/provision-worker.ps1`, `demo/project_description.md`

- [ ] **Step 1: SKILL.md** — the worker cycle (claim → render codex_task → `codex exec` background + notify → deploy via adapter → fetch holdout → validate → resume-on-behavioral-fail max 3 / abort-on-infra → `batch_done`), with literal `terminal(curl …)` shapes; sources `worker.env` by absolute path; preflight fails fast if a required env var is unset. (§8, §20.2)
- [ ] **Step 2: provision-worker.ps1** — per worker: profile dir (`HERMES_HOME`), config.yaml (gpt-5.6 via custom provider), `worker.env` (WORKER_ID, GATEWAY_URL=http://localhost:8090, N8N_URL, N8N_API_KEY, N8N_MCP_TOKEN, MAILPIT_URL, OPENAI_API_KEY), skill registration via `skills.external_dirs` (shared dir), a bland cron prompt, `approvals.cron_mode: approve`, a cron enable/disable switch. (§8)
- [ ] **Step 3: demo/project_description.md** — three separable deliverables (lead intake / follow-up / daily digest), each one deployable n8n workflow with its own trigger. Align-constraint: "one batch = one deployable n8n workflow with its own trigger". (§15)
- [ ] **Step 4 (acceptance):** provision ONE worker; confirm `N8N_MCP_TOKEN` reaches codex from inside a worker terminal call. Commit.

### Task 13: GATE B — single-worker E2E

- [ ] **Step 1:** `scripts/reset.sh`; run `captain_pipeline` on the demo description → verify 3 batches in the ledger.
- [ ] **Step 2:** start ONE worker; watch it take a batch to green `batch_done` end-to-end (claim → codex → deploy → validate → done), mirrored to Minibook.
- [ ] **GATE B DECISION (Fri evening):** single-worker E2E green → proceed to fleet. Else → cut to 1 worker + narrated architecture (story mode, §19); fleet becomes SHOULD.

---

## Phase 3 — Fleet, minibook mirror, autogen adapter & Gate C (Day 4, Sat 18)

Deliverable: an unattended 3-worker run producing three green batches + a complete Minibook thread.

### Task 14: Fleet ×3

- [ ] **Step 1:** run `provision-worker.ps1` for workers 2 and 3 (differ only in HERMES_HOME, workspace root, Minibook identity).
- [ ] **Step 2 (acceptance):** three workers poll and claim distinct batches with no double-execution (fencing holds). Commit any script fixes.

### Task 15: Minibook mirror + native run + registry feed

**Files:** Modify `minibook/config.yaml` (create), `gateway/mirror.py` (wire accounts)

- [ ] **Step 1: Minibook native** — `python run.py` (backend 8080) + the simple backend-served UI (NOT the Next.js build); author `config.yaml` with RAISED rate limits (defaults 60 comments/min, 10 posts/min would drop the burst mirror), port 8080, no external exposure. Note `require_admin` demo-disabled in README. (§12)
- [ ] **Step 2: Account provisioning** — register agent accounts (Captain, hermes-worker-N), store API keys in gateway env, create the demo project. Confirm `/api/v1/registry` routes serve without starting the SwarmPipeline forge runner. (§12, §23)
- [ ] **Step 3 (acceptance):** a full run's blocks appear as posts/comments with `Post.status` = validated/failed per batch (the green roll-up); `batch_done:succeeded` mirrors into `/api/v1/registry` as `status='validated'`; holdout is NOT mirrored. Commit.

### Task 16: autogen target adapter (COULD — only if fleet green)

**Files:** Create `agenten/adapters/autogen_adapter.py`, `templates/AGENTS.autogen.md`, `runtime/run_team.py`, `runtime/team_gate.py`, `tests/adapters/test_autogen_adapter.py`

**Interfaces:**
- Produces: `deploy()` (subprocess `build_team()` → `dump_component()` → validate `AutogenTeamConfig` → `team.json`), `execute()` (`run_team.py` imports `build_team()` fresh per case, `team.run(task)`), `observe()` (TaskResult → final_json/stop_reason/tool_calls/speakers + sink).

- [ ] **Step 1 (Codex build contract):** author the autogen adapter + `run_team.py` (executes `team.py`, NOT `team.json` — tools serialize under `config.workbench`, §21.2) + a FRESH ~30-line `team_gate.py` (ast.parse + `build_team()->BaseGroupChat` presence + secret regex + isolated `python -c "import team"`; do NOT vendor minibook's `test_generated_code`, §23) + `AGENTS.autogen.md` (framework lock, tools = validated n8n MCP tools via `autogen_ext.tools.mcp`, echo `{case_id}` in `final_json`). (§21.2, §21.1, §23)
- [ ] **Step 2 (acceptance test):** a lead-triage `SelectorGroupChat` team passes holdout assertions (`final_output.route`, `sink_called`, `tool_not_called{escalate}`, `termination_reason`); a non-serializing team fails `deploy()` before any case runs. `pytest tests/adapters/test_autogen_adapter.py`.
- [ ] **Step 3: Commit.**

### Task 17: Hardening + GATE C

- [ ] **Step 1:** heartbeats between worker steps; loud gateway warning on expiry-with-recent-blocks; terminal-state reject coverage. (§4, §5)
- [ ] **Step 2: GATE C (evening, unattended full dress):** reset → committed demo description → fleet ×3 → all three `batch_done` + complete Minibook thread, zero operator input. Red → Day 5 films the Gate-B single-worker config instead. (§16)

---

## Phase 4 — Footage, README, judge sandbox, video & submission (Days 5–6, Sun–Mon 19–20)

Deliverable: a public repo a judge can run + inspect, a <3-min video, and a complete Devpost submission.

### Task 18: Footage-capture runs (Day 5)

- [ ] **Step 1:** with demo caps (codex ≤8 min, ≤2 iterations), screen-record everything during full runs: codex terminal, Minibook timeline, n8n canvas, Mailpit inbox. Day 6 becomes edit-only. (§16)
- [ ] **Step 2:** capture the "self-healing" beat: a red validation → failure report → `codex exec resume` → green + personalized mail in Mailpit. (§16, §20)

### Task 19: README + repo cleanup + judge sandbox

**Files:** Create `README.md` (replace stub), `LICENSE`, `.env.example` (judge-facing), `runs/<ts>/` evidence snapshot; move `docs/superpowers/plans/` → keep, `test_claude_output.txt` → delete

- [ ] **Step 1: README** (English) — one-sentence narrative first ("one agent writes the spec AND the exam; another drives Codex until the exam passes; every step is in the ledger"); quickstart (compose + `.env.example` + one command per component); dev-session table + `/feedback` primary thread; PRIOR_WORK reference; license section (repo license + hermes MIT + minibook AGPL-3.0 via submodule, HTTP-only); "Supported platforms: Windows 11 (tested); Docker required for full run"; admin-auth demo-disabled note. (§16, §18)
- [ ] **Step 2: Judge sandbox** — commit one completed run's evidence (archived ledger export, n8n workflow JSONs, Mailpit export, Minibook DB snapshot) + a tiny read-only viewer command → testable WITHOUT rebuilding. (§16, §18)
- [ ] **Step 3: Cleanup** — delete `test_claude_output.txt`; real README title; English sweep over `docs/`. Commit.

### Task 20: Video (Day 6)

- [ ] **Step 1:** edit to <3 min; spoken narration MUST state how Codex + GPT-5.6 were used (§20.5 is the script); no third-party music/logos. Upload public to YouTube. (§16, §18)

### Task 21: Devpost submission (Day 6, buffer Tue 21)

- [ ] **Step 1:** fill the draft: category Developer Tools; text description; `/feedback` Session ID of the primary thread; video URL; repo URL (public, frozen through Aug 5); testing/sandbox instructions. (§18)
- [ ] **Step 2:** final checklist pass over spec §18; submit HOURS before 17:00 PT Mon 20 (Tue 21 is re-record/submit slack, not the target). (§16, §18)

---

## Self-review notes

- **Spec coverage:** §2→T1/T2/T19/T21; §4/§5→T5/T8; §6/§21.4→T9; §7/§21.2→T7/T10/T16; §8→T12/T14; §9/§10→T3/T10/T11; §11→T10; §12→T15; §13→global+T3; §14→T8; §15→T12; §16→phase structure + gates; §17 cut-lines→T16 COULD, story-mode at Gate B; §18→T19/T21; §19→gate decisions; §20/§20.5→T18/T20; §21→T9/T11/T16; §22→T5/T8 + vector note (not built); §23→T15/T16 (reuse registry, write gate fresh). 
- **Codex-authorship:** every core task (T5,6,8,9,10,11,16) is authored in the primary Codex thread with a session trailer; setup tasks (T1,2,3,12,15,19) are Claude/operator work.
- **Cut-lines under pressure:** T16 (autogen) and T14 (fleet) are the first to cut to story-mode; MUST path is T1–T13 + T18–T21.

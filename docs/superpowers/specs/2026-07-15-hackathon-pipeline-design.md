# Captain → Hermes → Codex Pipeline — Design Spec

**Purpose:** OpenAI Build Week submission (deadline 2026-07-21 17:00 PT, category
Developer Tools) built as a real extension of Captain_cook: a pipeline where the
Captain decomposes a project description into work batches, a fleet of Hermes agents
builds each batch by driving Codex CLI, and the result is validated against
measurable outcomes — every step recorded in the append-only ledger.

**Status:** Approved design (brainstormed 2026-07-14/15, hardened by a 30-agent
adversarial review — 24 confirmed findings + 19 notes — plus a 24-agent
completeness sweep — 15 confirmed gaps, incl. the mandatory `/feedback`
Session-ID form field; all folded in). Extended with the two-layer architecture
(§21: autogen-core 0.7.5 orchestrators over n8n tools), the autogen build
target (verified against installed 0.7.5 by a 6-agent panel — tool
serialization caveat included), the Hermes learning-loop stabilization (§21.3),
and MariaDB as the ledger store (§22).

---

## 1. Summary

```
Project description
      │
      ▼
┌──────────────┐   HTTP    ┌──────────────────┐   HTTP    ┌──────────────────┐
│   Captain     │──────────▶│  Ledger-Gateway  │◀──────────│  Hermes fleet     │
│ (AutoGen,     │           │ (FastAPI, SOLE   │           │ (N instances,     │
│  GPT-5.6,     │           │ ledger process;  │           │ captain-worker    │
│  own process) │           │ claims, fencing, │           │ skill, cron)      │
└──────────────┘           │ Minibook mirror, │           └────────┬─────────┘
                            │ mock-CRM sink)   │                    │ codex exec
                            └────────┬─────────┘                    ▼
                                     │                     ┌──────────────────┐
                            blockchain.json                │   Codex CLI       │
                            (hash-chained,                 │ (n8n MCP → SDK    │
                             append-only)                  │ code → workflow)  │
                                                           └────────┬─────────┘
        ┌──────────────────┐                                        ▼
        │  Minibook (UI)    │◀── read-model mirror     ┌──────────────────────┐
        │ agents' forum     │                          │ n8n (Docker) + Mailpit│
        └──────────────────┘                          │ deploy + validate     │
                                                       └──────────────────────┘
```

Judging story: **Codex is runtime architecture, not just a dev tool** — Hermes
workers schedule Codex builds programmatically; every Codex session ID is a
ledger block; the whole build history is auditable and watchable in Minibook.

## 2. Hackathon compliance (do FIRST, Day 0/1)

- **Prior-work bands (official cutoff = Submission Period start, 2026-07-13
  09:00 PT — verified in the rules):** commit the current tree and tag it
  `pre-codex-baseline`; the tag marks where Codex-authored hackathon work
  begins. `PRIOR_WORK.md` documents THREE timestamped bands: (1) commits
  before 2026-07-13 09:00 PT = prior work, not judged; (2) the 13–14.07
  Claude Code foundation (incl. decompose/judge from cf50e87) = in-period but
  non-Codex, disclosed honestly with commit timestamps; (3) everything after
  the tag = the Codex-authored work judges evaluate. Judges rate only
  in-period work — the Codex delta must carry the submission on its own.
- **Embedded repos (RESOLVED 2026-07-15):** minibook is now VENDORED at
  `./minibook` (PR #15 merged) — its full backend + frontend + swarm live in
  the main tree, so a clone gets it directly. The redundant `Autogen_AgentFarm`
  gitlink was dropped. Minibook is AGPL-3.0: its own `minibook/LICENSE` is
  kept, it is documented as a third-party component, and integration stays
  HTTP-only (no linking) — the README's license section states this. STILL
  OPEN: `hermes-agent/` is a bare gitlink with no `.gitmodules` (a clone gets
  an EMPTY dir) — decide vendor vs. proper submodule before submission
  (Day-1 task); `git clone --recursive` smoke test proves whichever fix.
- **Codex session capture (dev-time):** a shell wrapper (~20 lines, written as
  the FIRST Day-1 item, BEFORE the first Gate A `codex exec`) logs every dev
  session ID + date + intent to `docs/codex-sessions.md`; commits carry a
  `Codex-Session: <id>` trailer. Runtime sessions need no wrapper — they are
  captured as `codex_session` ledger blocks. Recovery: harvest
  `~/.codex/sessions/` rollout files (back this dir up daily). README leads
  with dev sessions; runtime thread_ids are the novelty layer on top. One
  honest sentence covers Claude Code's planning role.
- **Primary Codex thread (`/feedback` Session ID — MANDATORY form field):**
  the Devpost form requires the singular `/feedback` Codex Session ID of the
  thread "where the majority of core functionality was built". Therefore:
  designate ONE primary thread and `codex exec resume` it for the core
  gateway + pipeline implementation (Days 2–3) instead of fresh sessions per
  task; verify on Day 1 how `/feedback` is run against it (resume the thread
  in interactive Codex); record its ID in `docs/codex-sessions.md` the day it
  is created.
- **Devpost registration + draft (Day 1):** register the account, join the
  hackathon (prerequisite for the Resources-tab credits request), create a
  DRAFT submission immediately and copy every required form field into §18 —
  Day 6 becomes field-filling, not field-discovery. Note the entrant type
  (solo).
- **All new core implementation is authored in Codex sessions.** Claude Code
  plans/reviews; Codex writes the submitted code.
- **Model claim:** `config/llm_config.py` currently pins `gpt-4o`. Switch to
  `gpt-5.6` (env-overridable) on Day 1 and re-smoke-test decompose/judge (~1h).
  The demo run writes the resolved model name into the `project` block metadata
  so the ledger itself evidences the claim.
- **Credits:** request the $100 Codex credits via the Devpost Resources tab
  before Fri 2026-07-17 12:00 PT; credits expire 2026-07-31.
- **Freeze the in-flight refactor:** the boundary-refactor plan
  (`docs/superpowers/plans/2026-07-14-autogen-runtime-boundary-refactor.md`)
  is explicitly frozen before branching for the hackathon — no mid-week edits
  from two workstreams into the same files.

## 3. Components

1. **Captain** (existing + new stages) — standalone process, decomposes and
   enriches work batches. Section 6.
2. **Ledger-Gateway** (new, FastAPI) — the ONLY process that ever opens
   `blockchain.json`. Claims, fencing, schema validation, Minibook mirror,
   mock-CRM sink. Section 5.
3. **Hermes fleet** (new integration) — N hermes-agent instances running the
   `captain-worker` skill. Section 8.
4. **Codex CLI** (new integration) — headless builder driven by workers. Section 9.
5. **n8n 2.29.x + Mailpit** (Docker) — deploy target + mail catcher. Section 10.
6. **Minibook** (existing, submodule) — human-readable read model. Section 12.

## 4. Ledger protocol

### Block types

| type | writer | payload (all Pydantic-validated at the gateway) |
|---|---|---|
| `project` | Captain | refined description; metadata: resolved model name |
| `work_batch` | Captain | context bundle: goal, subtask ids, constraints, interfaces (dependency edges, §21), `target` (enum: `"n8n"` tool \| `"autogen"` orchestrator), acceptance criteria (assertion enum), **build-visible** golden cases |
| `holdout_cases` | Captain | validation-only cases; fetched by workers AFTER codex exec; never written into the workspace |
| `batch_claim` | Gateway | worker id, claim_token, expiry |
| `codex_task` | Hermes | verbatim prompt sent to codex |
| `codex_session` | Hermes | thread_id, exit status, artifact paths |
| `deploy` | Hermes | n8n workflow id, workflow name, webhook path |
| `validation_run` | Hermes | per case: assertion id, observed value, judge verdicts + reasons, rubric version, infra/behavioral classification |
| `batch_done` | Hermes | `succeeded` / `failed_after_max_iterations` / `aborted_infra`, iteration count |

Lifecycle: `work_batch` → `batch_claim` → (`codex_task` → `codex_session` →
`deploy` → `validation_run`) ×1..3 → `batch_done`. Children hang off the batch
via `parent_index`.

### Validation semantics (derived, tree-rollup)

The ledger is a TREE over the chain: `project` → N `work_batch` branch points
(one per built agent) → each branch carries that agent's full build + test
history. "Validated" is a DERIVED predicate, never a mutated field (hash
stability): an agent's branch is validated ⇔ its subtree contains a
`batch_done: succeeded` whose `validation_run` shows all holdout E2E cases
green. Roll-up rule, applied recursively: **a parent node is validated ⇔ all
its child branches are validated** — this week that is one level (project
validated ⇔ all 3 batches succeeded, derived by the gateway, shown in
Minibook as the tree turning green bottom-up); deeper agent hierarchies
(agents building sub-agents) attach as further branch levels under the same
rule, with `decompose`'s existing depth/`atomic` output as the natural seam.

### Claim fencing (review finding: double-execution was guaranteed without this)

- `POST /batches/{id}/claim` returns a **claim_token**; initial expiry **90 min**.
- Every WORKER-written child block (`codex_task`, `codex_session`, `deploy`,
  `validation_run`, `batch_done`) must carry the current token → else **409**.
  Captain's pre-claim writes (`work_batch`, `holdout_cases`) are explicitly
  unfenced — no claim exists yet.
- `POST /batches/{id}/claim/heartbeat` sets expiry = now + 30 min; the worker
  heartbeats between steps (a single codex step can block ~15 min, so +30 min
  per heartbeat keeps a margin above the longest step).
- Terminal batches accept NO further child blocks (409). First `batch_done` wins.
- **Expiry is lazy — no sweeper:** the claim endpoint and `GET /batches?status=pending`
  treat a claimed batch whose lease has expired as pending (re-claimable). Nothing
  else needs to run for "expiry frees the batch" to be true.
- Gateway logs a loud warning when a claim expires shortly after blocks arrived
  from the old holder (observability for this bug class).

### Hash stability (review finding: chain links broke by design)

`compute_hash` currently includes mutable fields; appending a child re-hashes the
parent and breaks `previous_hash` links. Fix (one-liner, Codex-authored): exclude
mutable fields (children, status) from the hash — block hashes are stable from
creation. The new protocol never uses `update_task_status`; status changes ARE
blocks (`batch_claim`, `batch_done`). Public phrasing: **"append-only audited
ledger with per-block hashes"** — never "tamper-proof blockchain". No chain
verification work this week.

## 5. Ledger-Gateway (FastAPI)

> **Superseded by the MariaDB decision (§22):** the ledger is a MariaDB store,
> not a JSON file. The file-based mitigations below (exclusive lock file,
> corrupt-rename, "only one process opens the file") are therefore UNNECESSARY
> — transactional writes and row-locking replace them. What survives: the
> gateway is the sole WRITER (readers connect directly), claim fencing is an
> atomic transactional compare-and-set, and `CaptainAgent` still takes an
> injected `LedgerClient` (HTTP) rather than constructing its own store. Read
> the rest of this section as the rationale for single-writer discipline; the
> mechanism is now the DB, not a lock file.

**Single-owner rule (review finding: two processes corrupt the ledger file):**

- The gateway is the ONLY process constructing `Blockchain` over the shared file
  for its whole lifetime. Enforced by: (a) an exclusive lock file next to
  `blockchain.json` (`os.open(..., O_CREAT|O_EXCL)`, PID inside, cleaned on
  exit); (b) `CaptainAgent` loses its eager `Blockchain()` construction and takes
  an injected `LedgerClient` (below); (c) an import-boundary test (pattern:
  `tests/test_import_boundaries.py`) asserting only the gateway package imports
  `Blockchain_modell`/`JSONFileStorage` — with an explicit whitelist for the
  legacy `agenten/ledger_bridge/` package (recorder/CQRS pipeline, out of
  scope, imports it today). `Captain.py`'s own `Blockchain` import (line 6)
  is removed as part of the seam; `agenten/functions/vizualisation.py` reads
  `blockchain.json` directly (read-only, tolerable this week).
- `JSONFileStorage.load`: on `JSONDecodeError`, rename the file to
  `blockchain.json.corrupt-<ts>` and **raise** — never silently re-genesis.
  Gateway refuses to create a fresh ledger unless started with `--init`.
- The gateway does **not** wrap the supply-chain recorder (it is enqueue-only,
  different pipeline, different stage vocabulary — out of scope). The gateway
  calls `Blockchain.add_block` directly and keeps its own `dict[status, set[index]]`
  status index (linear scan acceptable at demo scale).
- All write endpoints are `async def` on a single event loop (uvicorn
  `workers=1`) with one shared `asyncio.Lock` around check-and-set sections —
  plain `def` handlers would run in a threadpool and reintroduce the race.

**Endpoints:**

```
GET  /batches?status=pending            → [{batch_id, title}] (ids only)
POST /batches/{id}/claim                → claim_token | 409
POST /batches/{id}/claim/heartbeat      extend expiry
POST /batches/{id}/approve              pending_review → pending (flag-gated)
POST /blocks                            schema-validated write (worker block
                                        types fenced by claim token)
GET  /batches/{id}/bundle               work_batch payload — holdout EXCLUDED
GET  /batches/{id}/blocks               batch subtree — holdout EXCLUDED
GET  /batches/{id}/holdout              claim-token-fenced; served ONLY after a
                                        codex_session block exists for the
                                        current iteration → holdout isolation
                                        is API-enforced, not merely
                                        SKILL.md-discouraged
POST /sink/crm                          mock-CRM sink: stores {case_id, tag, ...}
GET  /sink/crm?case_id=...              validator reads observed sink calls
```

**LedgerClient seam:** `add_block/get_blocks` protocol with two impls —
`GatewayHTTPClient` (production) and `DirectBlockchainClient` (tests only).
Captain, workers, and any dashboard go through HTTP. (~2–4h migration:
`Captain.py` lines 27/32–36, `main.py`, one test site.)

**Mock-CRM sink:** gives "CRM tag" outcomes an observation channel (stack is
n8n + Mailpit only). n8n reaches it via `host.docker.internal:8090`; AGENTS.md
carves an explicit exception to the no-external-side-effects rule for the sink
URL and requires echoing `case_id` in every sink POST.

**Minibook mirror:** fire-and-forget — an in-process queue; mirror failures are
logged and NEVER block or fail a ledger write. Mapping in section 12.

## 6. Captain pipeline

**Style (review finding: registry can't do structured output or loops):**
`align_batches` / `enrich_batch` are NOT registry workflows. They follow
`agenten/llm/decompose.py`: factory functions in `agenten/llm/`
(`make_align_batches(model_client)`, `make_enrich_batch(model_client)`), fresh
`AssistantAgent` per call, `output_content_type=<Pydantic model>`, raise on
non-structured response. A plain-Python driver `agenten/pipeline/captain_pipeline.py`
(main.py-style, standalone process — avoids the `NestedChatWorkflow.run`
event-loop trap) runs:

1. `project_definition` (existing registry workflow, unchanged)
2. `decompose` (existing, cf50e87)
3. `align` → **deterministic checks in code** (every subtask id in exactly one
   batch; set equality for coverage) — regenerate on failure without spending a
   judge call; then an LLM judge only for the fuzzy criterion "each batch
   independently buildable as ONE n8n workflow with its own trigger"; max 2
   rounds. Note: the existing `make_llm_judge` is constitution-bound and calls
   `model_client.create()` directly — the align judge is NEW code following
   that pattern, not a drop-in reuse.
4. per batch: `enrich` → context bundle + assertion-enum acceptance criteria +
   golden cases + holdout cases. Deterministic cross-check: every interface
   mentioned resolves to an existing batch id.
5. `release` → `work_batch` + `holdout_cases` blocks via `GatewayHTTPClient`.
   With `--review`: released as `pending_review` (gateway approve endpoint flips
   to `pending`); default auto for the demo. No UI.

**Target selection:** `target` is an enum with a single member `"n8n"`, populated
from pipeline config — the LLM does not choose this week. The enum in the schema
is the documented extensibility point.

## 7. Assertion vocabulary & golden dataset

**Closed enum, bound 1:1 to observation channels** (review finding: runData/
node-name assertions are impossible without implementation knowledge). Frozen
Day 1/2 as the Captain↔adapter interface, shared Pydantic module:

```
mail_sent{to, subject_contains?, body_must_contain_fields[]}
no_mail{to}
webhook_response{status, json_path_equals: {route: hot|lukewarm|reject, case_id}}
sink_called{json_path_equals}          # mock-CRM
execution_status{value}
```

Unobservable assertions are impossible by construction (enum validation), not
merely discouraged in the prompt.

**Correlation (mandated by contract, not inferred):** validator injects a UUID
`case_id` into every test payload; AGENTS.md requires the workflow to echo
`{case_id, route}` in the webhook response body and to suffix mail subjects with
`[case:<id>]`. Per-case recipient addressing
`case{n}.b{batch_id}.i{iteration}@demo.local` isolates concurrent workers'
Mailpit traffic (search by `to:`); cases run sequentially. Execution
correlation: poll `GET /api/v1/executions?workflowId=` (no `includeData`) with
backoff (2s, max 60s), then fetch candidates with `includeData=true` and match
`case_id` in the webhook node's recorded input; require terminal status.

**Anti-reward-hacking (builder must not see the exam):**
- Golden cases split: **build-visible** examples in `work_batch` (Codex may see
  them) vs **holdout** cases in `holdout_cases`, fetched by the worker only
  after `codex exec` returns and never written into the workspace.
- Failure reports name the failed assertion + case *category*, never case
  identifiers or payloads. If iteration 3 still fails, a fuller report is
  allowed only on a flagged, non-counting diagnostic run.
- Backstop: normalized literal-sniff of built artifacts for holdout values.

Thresholds: hard assertions must pass on ALL holdout cases. Soft semantic
checks: the judge scores each sub-criterion pass/fail with a reason (rubric
v1, version string stored in `work_batch`); a case passes its soft checks at
≥ 80% sub-criteria, and the batch passes when every case passes. Judge
evidence (verdicts + reasons per sub-criterion) persists in `validation_run`.
Stretch hardening (first COULD item): a generalization re-run that mutates
surface values of the holdout cases (names, companies) to catch semantic
hardcoding the literal-sniff backstop misses.

## 8. Hermes fleet

**Worker cycle** (`captain-worker` skill; cron fires ~60s):

1. `GET /batches?status=pending` → none: sleep.
2. `POST claim` (409 → step 1). Heartbeat after every following step.
3. Read context bundle; render Codex prompt from a fixed template → `codex_task`.
4. Run codex (section 9) **as a background process with completion polling** —
   review finding: the hermes cron path stacks ~10-min/3-min foreground
   ceilings that would kill 15-min codex runs. Collect thread_id, exit code,
   artifacts → `codex_session`. 15-min timeout.
5. Deploy via the target adapter → `deploy` block.
6. Fetch holdout cases; validate (section 11); on behavioral fail → failure
   report → `codex exec resume <thread_id>` (fallback: fresh exec with artifact
   + report), max 3 iterations. Infra failures (ECONNREFUSED, credential-not-
   found at the mail node) → `batch_done: aborted_infra` immediately — never
   burn codex iterations on infrastructure.
7. `batch_done`; archive workspace log; loop.

**Cron & skill mechanics (review-verified against vendored code):**
- Cron prompt is one bland sentence (the create-time injection scanner rejects
  curl+`$TOKEN` patterns); all mechanics live in SKILL.md + helper scripts.
- `approvals.cron_mode: approve` in each worker profile.
- Skill registered via `skills.external_dirs` in hermes `config.yaml` (edits in
  the repo take effect without re-syncing to `~/.hermes/skills`).
- Fleet provisioning: `provision-worker.ps1` (Day 3, idempotent) creates per
  worker: profile dir (`HERMES_HOME`), config.yaml, skill registration, cron
  job, gateway process. Workers differ only in HERMES_HOME, workspace root,
  Minibook identity. `HERMES_HOME` propagated explicitly to subprocesses.
- Target adapter interface (universality seam): `deploy(artifact)`,
  `execute(test_case)`, `observe()` — n8n adapter this week; interface
  documented for jira/code adapters. Stretch only: trivial `code` adapter.
- **Worker runtime environment contract:** `provision-worker.ps1` writes a
  per-profile `worker.env` (WORKER_ID, GATEWAY_URL=http://localhost:8090,
  N8N_URL, N8N_API_KEY, N8N_MCP_TOKEN, MAILPIT_URL, OPENAI_API_KEY for the
  soft-check judge). Every helper script in `skills/captain-worker/scripts/`
  sources it via ABSOLUTE path as its first line — no reliance on hermes
  inheriting profile `.env` into terminal subprocesses — and the codex launch
  line explicitly exports `N8N_MCP_TOKEN` before `codex exec`. A skill
  preflight fails fast with a clear message if any var is unset (otherwise
  MCP failures masquerade as behavioral failures and burn codex iterations).
  SKILL.md curl examples are written for Git-Bash-on-Windows quoting; where
  quoting gets hairy, helper scripts use Python instead. Note: the per-worker
  "gateway process" in provisioning refers to the HERMES gateway daemon — not
  the (singleton) Ledger-Gateway.

## 9. Codex integration

- **Invocation:** `codex exec --json -C C:/Users/User/Desktop/Captain_cook/workspaces/<batch-id>`
  (forward-slash Windows paths — MSYS bash mangles POSIX-style ones). Workspaces
  live INSIDE the trusted repo (gitignored) → project trust is inherited; also
  avoids `--skip-git-repo-check`.
- **Session id:** `thread_id` from the `thread.started` JSONL event. Resume:
  `codex exec resume <thread_id>` (verify Day 1; stateless fallback documented).
- **Sandbox/approval posture on the command line, not in config:** start with
  `--sandbox workspace-write`; if MCP calls stall (issue #24135 approval
  auto-cancel), fall back to the machine's global `approval_policy=never` +
  `danger-full-access` — acceptable: isolated workspaces, local-only targets,
  no secrets in codex env. The shipped posture is documented in the README.
- **MCP config:** registered globally (`codex mcp add n8n --url
  http://localhost:5678/mcp-server/http --bearer-token-env-var N8N_MCP_TOKEN`)
  AND mirrored in workspace `.codex/config.toml`. `default_tools_approval_mode
  = "auto"` per server.
- **Build path (primary):** official n8n instance-level MCP; artifact = n8n
  **Workflow SDK TypeScript code** (review finding: NOT workflow JSON) —
  `get_sdk_reference`/`search_nodes`/`get_node_types` → `validate_workflow` →
  `create_workflow_from_code` → `publish_workflow`. czlonkowski/n8n-mcp is CUT
  from the default config (one stdio spawn risk fewer).
- **Build path (fallback, decided at Gate A):** artifact = workflow JSON in the
  workspace + static node docs + 2–3 example workflows; hermes deploys via
  public REST.
- **AGENTS.md template (per workspace)** — the build contract: target artifact
  and path; MUST include a webhook trigger at path `/hook/{batch_id}`;
  deterministic workflow name derived from batch id; every routing branch ends
  in a distinguishable observable (mail template / no action / sink call);
  webhook response echoes `{case_id, route}`; mail subjects carry `[case:<id>]`;
  use existing credential `mailpit-smtp` — NEVER create SMTP credentials or set
  SMTP hosts; LLM nodes (e.g. personalized mail drafting) use ONLY the
  pre-provisioned `openai-gpt` credential (GPT-5.6) — never inline API keys;
  node prompts must be derived from the batch context bundle (goal/constraints),
  no invented business rules; no external side effects except the provided sink
  URL; sink POSTs echo `case_id`.
- **Identity & derived names (single source of truth):** `batch_id` is the
  `AlignResponse.batch_id` slug, validated `^[a-z0-9-]{1,32}$` at release.
  Literal derivations live ONCE in the shared constants module: workflow name
  `captain-batch-{batch_id}`, webhook path `hook/{batch_id}`, workspace
  `workspaces/{batch_id}`. The worker renders the PRE-COMPUTED literals into
  AGENTS.md — Codex copies strings, it never implements a derivation.
  Workflows without branching echo a fixed `route: "processed"` (the
  `{case_id, route}` echo stays uniform across all three demo workflows).

## 10. n8n + Mailpit deployment

- docker-compose: n8n pinned 2.29.x + Mailpit + **MariaDB** (§22, the ledger
  store) only (Minibook runs natively — no Dockerfile exists and none will be
  written this week). n8n keeps its own SQLite volume (it can't use MariaDB).
- **Bootstrap:** owner account provisioned via env
  (`N8N_INSTANCE_OWNER_MANAGED_BY_ENV` + email/name/password-hash), MCP enabled
  via env (`N8N_MCP_MANAGED_BY_ENV=true`, `N8N_MCP_ACCESS_ENABLED=true`).
  Remaining manual (once, Day 1): create the API key + copy the MCP access
  token from the UI. Persist the docker volume — **never delete it** (the API
  key dies with it). Pre-provision two n8n credentials: `mailpit-smtp` (mail)
  and `openai-gpt` (GPT-5.6, for LLM nodes in built workflows — the built
  artifact itself runs on GPT-5.6).
- **Adapter `deploy()` is per-build-path** (resolves an ambiguity found in
  verification):
  - **MCP path (primary):** Codex itself creates + publishes via
    `create_workflow_from_code`/`publish_workflow`. `deploy()` is then
    **verify-and-adopt**: look up the workflow by its deterministic name via
    REST, confirm it is published, record the id, and delete stale duplicates
    from earlier iterations/workers. It never re-creates what Codex published.
  - **JSON fallback path:** full REST **upsert** (find by deterministic name →
    deactivate+delete → create+activate), with the create traps handled: strip
    read-only fields (`id`, `versionId`, `tags`), ensure `settings: {}`;
    activation is a SEPARATE `POST /workflows/{id}/activate` and is the
    publish gate.
  Both paths end with the same postcondition (exactly one published workflow
  with the deterministic name), keeping retries and iterations idempotent.
  No execute-by-id endpoint exists — execution happens via webhook POST only.
- Fixed ports in one shared `.env`: gateway 8090, minibook 8080, n8n 5678,
  mailpit 8025/1025.

## 11. Validation loop (per iteration)

1. Adapter `deploy()` (per-path, §10) → publish gate passed.
2. For each holdout case (sequential): `execute()` = POST payload (with
   `case_id`, per-case recipient) to `/hook/{batch_id}` webhook.
3. `observe()`: webhook response fields; executions API (correlated by
   `case_id`); Mailpit search (`to:` per-case recipient); mock-CRM sink query.
   Mail assertions poll Mailpit with backoff up to 30 s per case; `no_mail`
   is evaluated only after that window closes.
4. Evaluate assertions (enum) + LLM-judge soft checks → `validation_run` block
   with full evidence.
5. Classify failures: **infra** (connection/credential errors) → abort batch as
   `aborted_infra`; **behavioral** → failure report (assertion + category only)
   → `codex exec resume`. Max 3 iterations → `batch_done`.

## 12. Minibook mirror

- Read model ONLY; never writes back to the ledger; mirror is fire-and-forget.
- Provisioning script (Day 4): register agent accounts (Captain,
  hermes-worker-N), store API keys in gateway env, create the demo project.
- Mapping: `project` → project creation + intro post by Captain; `work_batch`
  → post (`Post.status` carries the batch's derived validation state:
  in_progress → validated/failed, giving the "tree turns green" signal);
  claim/codex_task (prompt summary)/codex_session/deploy/validation_run/
  batch_done → comments by the acting agent's account with @mentions;
  `holdout_cases` is NOT mirrored (exam stays private). Posts reference block
  **index + type + stable hash**.
- Demo footage films the forum of a COMPLETED run (never live).

**Gap assessment (minibook is feature-complete for the mirror; gaps are glue
+ ops + one visualization decision — verified against `src/main.py`,
`ratelimit.py`):**
- **Config required:** no `config.yaml` exists → author one Day 4 with RAISED
  rate limits (defaults are 60 comments/min, 10 posts/min PER AGENT; a burst
  mirror would otherwise 429 and, being fire-and-forget, silently DROP
  comments → an incomplete filmed forum), port 8080, no external exposure.
- **One frontend only:** use the backend-served simple UI on 8080 — do NOT
  build the Next.js `frontend/` (README's 3456/3457 stack) unless the simple
  UI proves un-filmable. This overrides the earlier "frontend build" wording.
- **Tree/roll-up view:** minibook is a forum, not a tree. The green roll-up
  (§4) rides on `Post.status` per batch; the actual TREE visual is a separate
  Graphviz render of the ledger (COULD — matches the branch's intent),
  not a minibook feature.
- **Delivery — RESOLVED 2026-07-15:** minibook is VENDORED at `./minibook`
  (PR #15 merged); the redundant `Autogen_AgentFarm` gitlink was dropped. All
  paths below are `minibook/...` (not `Autogen_AgentFarm/minibook/...`).
- **`require_admin` returns `True` (TODO stub):** harmless on localhost, but
  the repo is public — one README line notes admin auth is demo-disabled.
- **Reuse bonus (optional):** minibook already has `/api/v1/registry`
  (validated agent teams, `eval_score >= 6` gate, capabilities/mcp_servers).
  Mirroring `batch_done: succeeded` there too strengthens the
  "agents self-register once validated" story — COULD, not required.

## 13. Secrets

| secret | lives in | must never reach |
|---|---|---|
| `OPENAI_API_KEY` (GPT-5.6 Captain) | `.env` → Captain process env | ledger, Minibook, codex workspace |
| n8n API key | `.env` → gateway + adapter env | ledger, Minibook, codex workspace, cron prompts |
| n8n MCP access token (`N8N_MCP_TOKEN`) | persisted Day 1 as user-level env var (`setx`) for dev shells; per-profile `worker.env` for workers; codex reads it via `bearer_token_env_var` indirection | ledger, Minibook, cron prompts |
| `OPENAI_API_KEY` (soft-check judge, `validate.py`) | per-profile `worker.env`, sourced by helper scripts via absolute path | ledger, Minibook, codex workspace |
| Minibook agent API keys | gateway env | ledger, codex workspace |
| Mailpit | none (open, local) | — |

Rule enforced in review of every block schema: payloads contain no secret
fields; codex workspaces get only `N8N_MCP_TOKEN` via MCP config indirection.

## 14. Startup ordering & failure policy

Order: docker-compose up (n8n healthy, Mailpit healthy — `docker info`
preflight) → gateway (acquires lock; `--init` only on first run) → Captain
pipeline run (writes batches) → workers.

Worker vs gateway outage: retry with backoff (3 attempts) then abort the CYCLE
(not the batch) — claim expiry frees the batch later. n8n outage during
validation → infra classification → `aborted_infra`.

**Reset script** (Day 2, ~30 lines): disable all worker crons → stop workers →
archive `blockchain.json` AND `workspaces/` to `runs/<ts>/` → start fresh →
delete all n8n workflows via API → wipe Mailpit (`DELETE /api/v1/messages`) →
optionally wipe `minibook.db`. Run before every E2E and before filming. Never
touches the n8n docker volume.

**Workspace lifecycle:** on a FRESH claim the worker deletes and recreates
`workspaces/<batch-id>` before copying AGENTS.md in; iterations within the
same claim reuse it (`codex exec resume` needs the artifacts). `.gitignore`
gains `workspaces/` and `runs/` on Day 1.

**Idle-burn rule:** worker crons run ONLY during E2E/filming windows — outside
them they stay disabled (a 60s GPT-5.6 polling fleet is real money);
`provision-worker.ps1` gets an enable/disable switch and the reset script
disables all crons as its first step.

## 15. Demo scenario (tested artifact, not ad-lib input)

The project description is committed as a file and TESTED on Day 3 (must align
to exactly 3 batches). It names three separable deliverables, each one
deployable n8n workflow with its own trigger and observable outcomes:

1. Lead intake: score + route incoming leads (webhook → route → Mailpit/sink)
2. Follow-up: personalized outreach for hot leads (webhook → Mailpit)
3. Daily digest: lead report summary (webhook-triggered for the demo → Mailpit)

Align-prompt constraint: "one batch = one deployable n8n workflow with its own
trigger". If real CRM semantics are wanted in the story, they appear as the
mock-CRM sink (observable), stated in the description.

## 16. Week plan & gates

- **Day 1 (Wed 15., today):** FIRST: session-capture wrapper +
  `docs/codex-sessions.md` + `Codex-Session` trailer adopted — BEFORE the
  first `codex exec`. Devpost: register, join hackathon, create draft
  submission, copy form fields into §18; credits requested. Model→gpt-5.6 +
  smoke test; baseline tag + PRIOR_WORK.md (three bands, §2) + submodule fix
  (+ `clone --recursive` proof); `.gitignore` += `workspaces/`, `runs/`;
  author repo `docker-compose.yml` (n8n + Mailpit + MariaDB) + up + `MariaDBStorage`
  behind LedgerStorage + `PyMySQL` pin; autogen spike: dump a team with a
  FunctionTool, confirm where tools serialize (`config.workbench`) and whether a
  round-trip executes them, and that a keyless `OpenAIChatCompletionClient`
  dumps `api_key: null` (§21.2) — freezes the autogen schema decision; n8n
  bootstrap +
  `mailpit-smtp`/`openai-gpt` credentials + `setx N8N_MCP_TOKEN`; minimal
  `templates/AGENTS.md` (credential + webhook rules only) for Gate A
  workspaces. **GATE A (binary, scripted):** fresh workspace → codex exec
  builds the trivial workflow (MCP path; n8n tool names visible in the JSON
  event stream) → deployed → webhook POST → mail visible in Mailpit —
  **twice in a row**. MCP experiments timeboxed to the morning, 15:00
  go/no-go → else switch to the JSON+REST fallback path (Gate A also
  exercises the REST deploy once via curl so the fallback is proven, not
  assumed). Verify `codex exec resume`, the `/feedback` flow (§2), and a
  12-min codex run under hermes cron (background mode). Budget check covers
  BOTH codex (~50–100 exec turns; else flip auth to the API key) AND the API
  key's tier vs. the fleet's idle GPT-5.6 polling. Hermes single instance
  installed + manually provisioned (profile, config.yaml, .env — the Day-3
  script automates what Day 1 does by hand).
- **Day 2 (Thu 16.):** gateway (single-owner enforcement, fencing, sink,
  schemas, tests) — direct `Blockchain` writes, `async def` + lock; pin
  fastapi/uvicorn in requirements.txt; hash stability fix; LedgerClient seam
  in Captain; assertion-enum module frozen; reset script.
- **Day 3 (Fri 17.):** ⚠ credits deadline 12:00 PT. captain-worker skill +
  n8n adapter + full `templates/` trio (`AGENTS.md` contract per §9,
  `codex_task.md`, `failure_report.md`) + `scripts/validate.py` (correlation
  polling, Mailpit/sink observation, soft-check judge, literal-sniff) on ONE
  hermes instance; smoke-verify `N8N_MCP_TOKEN` reaches codex from inside a
  worker terminal call; Captain align/enrich + pipeline driver (documented
  run command:
  `python -m agenten.pipeline.captain_pipeline demo/project_description.md`);
  demo description tested → 3 batches; write `provision-worker.ps1` (used
  Day 4). **GATE B:** single-worker E2E green by evening — else fleet is cut
  to 1 worker + narrated architecture (story mode).
- **Day 4 (Sat 18.):** Minibook native install + start (`python run.py` +
  frontend build, port 8080); fleet ×3 via provision-worker.ps1; Minibook
  mirror + account provisioning; hardening (heartbeats, expiry warnings,
  terminal-state rejects). **GATE C (evening, unattended full dress):**
  reset → committed demo description → fleet ×3 → all three `batch_done` +
  complete Minibook thread with zero operator input. Red → Day 5 films the
  Gate-B single-worker configuration instead of debugging the fleet under
  time pressure.
- **Day 5 (Sun 19.):** footage-capture runs (codex terminal, Minibook, n8n
  canvas, Mailpit) — screen-record everything; README (English, quickstart for
  judges, session table, tooling honesty, AGPL note, license section);
  repo cleanup (`test_claude_output.txt` out, plan docs → `docs/planning/`,
  real title); video script written in the evening — leads with the
  "Captain writes the spec AND the exam" loop; cut from the script:
  hash-chain internals, CQRS, recovery, fleet mechanics (README screenshots
  instead). Forum footage is always of a COMPLETED run. **Judge sandbox
  committed:** one completed run's archived ledger (`runs/<ts>/`), exported
  workflow JSONs, Mailpit export, Minibook DB snapshot + a tiny read-only
  viewer command — judges inspect a real end-to-end run in 2 minutes, zero
  keys, zero Docker (Dev-Tools track requires testability WITHOUT
  rebuilding). Submission repo is `github.com/Flissel/Captain_cook` (ALREADY
  public — no secrets may ever be committed, all week); `main` is the
  submission branch (judges clone the default branch), hackathon work merges
  in via PRs, `pre-codex-baseline` is pushed explicitly
  (`git push origin pre-codex-baseline`). LICENSE file + judge-facing
  `.env.example` committed; README states "Supported platforms: Windows 11
  (tested); Docker required for full run".
- **Day 6 (Mon 20.):** edit + upload video (<3 min, YouTube public). Video
  RULES: spoken narration MUST state how Codex and GPT-5.6 were used (§20.5
  is the ready-made script); no background music unless royalty-free/owned;
  no third-party logos beyond functional app UI. Devpost form draft complete.
  After submission the repo stays public and FROZEN until judging ends
  (Aug 5) — judges clone what was submitted.
- **Buffer (Tue 21.):** re-record slack if Day-5 footage is unusable; **submit
  hours before the 17:00 PT deadline**, not at the wire.
- Demo-run caps: codex ≤8 min, ≤2 iterations for filmed runs.

## 17. Scope cut-lines

- **MUST:** gateway + protocol + fencing; Captain pipeline (align/enrich);
  ONE hermes worker E2E; codex build path (either variant); n8n adapter +
  validation with holdout; reset script; compliance artifacts (tag,
  PRIOR_WORK.md, submodules, session log); README + video.
- **SHOULD:** fleet of 3; Minibook mirror; review-gate flag; mock-CRM sink
  (downgrade: fold CRM outcome into webhook_response field if time is short).
- **COULD (in order):** holdout generalization re-run (mutated surface
  values, §7); czlonkowski fallback config; `code` target adapter;
  judge-rubric polish.
  Cut bottom-up under pressure; never cut MUST for SHOULD polish.

## 18. Devpost submission checklist

- [ ] Devpost: registered, hackathon joined, DRAFT submission created Day 1
- [ ] Category: Developer Tools; project name matches README title
- [ ] Text description (English): features + how Codex and GPT-5.6 were used
- [ ] **`/feedback` Codex Session ID** of the primary core-implementation
      thread → its own MANDATORY form field (thread strategy in §2)
- [ ] Video: <3 min, public YouTube, clear audio, shows the project working;
      spoken narration explicitly covers Codex + GPT-5.6 usage; no
      third-party copyrighted content (music/logos)
- [ ] Repo public through judging (Aug 5), then frozen; README quickstart lets
      judges run it (compose + `.env.example` + one command per component)
- [ ] Judge sandbox: committed evidence of a completed run (archived ledger,
      workflow JSONs, Mailpit export, Minibook snapshot + viewer) — testable
      WITHOUT rebuilding; supported platforms stated
- [ ] Codex Session IDs: dev-session table in README + `docs/codex-sessions.md`
      + commit trailers; runtime thread_ids shown as ledger blocks
- [ ] PRIOR_WORK.md (three bands, §2) + `pre-codex-baseline` tag referenced in
      README
- [ ] License section: repo license + third-party notes (hermes MIT, minibook
      AGPL-3.0 via submodule, HTTP-only)
- [ ] All materials English; `$100` credits used before 31.07

## 19. Risks & fallbacks

| risk | mitigation / fallback |
|---|---|
| GPT-5.6 struggles with SDK-code builds via MCP | Gate A go/no-go → JSON+REST path |
| MCP approval auto-cancel headless (#24135) | approval_mode=auto → global bypass posture, documented |
| Hermes multi-instance on Windows unstable | Gate B → 1 worker + story mode; fleet is SHOULD, not MUST |
| Validation non-convergence | max 3 iterations; `failed_after_max_iterations` is a legitimate, auditable outcome |
| Ledger corruption | single-owner + lock + corrupt-rename; archive per run |
| Timeline slip | cut-lines (section 17); gates force the decision early |

## 20. Agent, prompt & tool inventory

Who speaks with which model, from which prompt file, with which tools, invoked
by whom — the operational map the architecture sections assume.

### 20.1 LLM touchpoints

| # | role | model | system-prompt source | in → out | invoked by / when |
|---|---|---|---|---|---|
| 1 | project_definition (generator / critic / structured_output) | GPT-5.6 | `agenten/workflows/project_definition.py` (existing registry workflow; step texts get a cleanup pass — several are garbled/typo'd and submission-visible) | `{project_description}` → refined description (text) | `captain_pipeline` stage 1 |
| 2 | decompose | GPT-5.6 | `agenten/llm/decompose.py::_build_system_message` (existing; we inject our own capability tag set, e.g. `["n8n_workflow"]`) | refined description → `DecomposeResponse{subproblems}` | pipeline stage 2 |
| 3 | align_batches | GPT-5.6 | NEW `agenten/llm/align.py` (prompt colocated, decompose.py pattern) | subtask list → `AlignResponse{batches: [{batch_id, title, subtask_ids}]}` | pipeline stage 3 |
| 4 | align judge | GPT-5.6 | NEW `agenten/llm/align_judge.py` (pattern of `judge.py`, NOT constitution-bound) | grouping + description → `JudgeVerdict{accept, reason}` | stage-3 loop, ≤ 2 rounds, only after deterministic checks pass |
| 5 | enrich_batch | GPT-5.6 | NEW `agenten/llm/enrich.py` | batch + description → `ContextBundle` (goal, constraints, interfaces, assertions, golden + holdout cases) | pipeline stage 4, per batch |
| 6 | Hermes worker agent | GPT-5.6 via direct OpenAI API (profile `config.yaml`: `model.provider: custom`, `base_url: https://api.openai.com/v1`, `default: gpt-5.6`; key in profile `.env`; hermes auto-selects the Responses API for GPT-5.x) | Hermes 3-tier prompt: worker persona in profile `SOUL.md` + skills index (stable) · workdir `AGENTS.md` (context) · `captain-worker` SKILL.md attached per cron job via its `skills` param | cron wake → batch processed | internal hermes cron (~60s) + terminal completion notifications |
| 7 | Codex | Codex CLI's own model | rendered `templates/codex_task.md` + workspace `AGENTS.md` (layered instructions) | build task → workflow via n8n MCP | worker step 4: `terminal(command="codex exec …", background=true, notify_on_complete=true)` — completion wakes the agent; NO foreground call (600s hard cap) |
| 8 | soft-check judge | GPT-5.6 | rubric v1 in the shared assertion module; called from `scripts/validate.py` via the OpenAI SDK directly (deterministic harness, NOT an agent) | observations per case → per-sub-criterion pass/fail + reason | validation step, per case with soft assertions |

### 20.2 Tool provenance

| runtime | tools | source / registration |
|---|---|---|
| Captain pipeline | model client only (existing `Tool` registry incl. `InternetSearchTool` stays unused this week) | `agenten/llm/model_client.py` |
| Ledger-Gateway | none (no LLM) | — |
| Hermes worker | built-ins: `terminal`+`process`, `read_file`/`write_file`/`patch`/`search_files`, `cronjob`; cron jobs pinned to `enabled_toolsets: [terminal, file]`; **no HTTP tool exists** — every gateway/n8n/Mailpit REST call is `terminal(curl …)`, stated explicitly with literal call shapes in SKILL.md | hermes tool registry; skill + helper scripts in the worker profile's `skills/captain-worker/` |
| Codex | n8n MCP tools (`get_sdk_reference`, `search_nodes`, `get_node_types`, `validate_workflow`, `create_workflow_from_code`, `update_workflow`, `publish_workflow`, `test_workflow`, …) + sandboxed shell/files in the workspace | global `codex mcp add n8n …` + workspace `.codex/config.toml` (§9) |
| Minibook | none — the mirror is gateway-side HTTP, no agent involved | — |

### 20.3 Prompt & instruction artifacts (all files, all English)

| artifact | path | owner / rendered when |
|---|---|---|
| existing workflow prompts | `agenten/workflows/project_definition.py` | prior work; cleanup pass Day 3 |
| decompose / align / align-judge / enrich prompts | `agenten/llm/{decompose,align,align_judge,enrich}.py` | align/judge/enrich are NEW (Codex-authored) |
| assertion enum + rubric v1 | `agenten/validation/assertions.py` (shared Captain ↔ adapter module) | frozen Day 2 |
| codex build prompt template | `templates/codex_task.md` | rendered per batch by the worker (step 3) |
| workspace build contract | `templates/AGENTS.md` → copied into each batch workspace | rendered per batch; content per §9 |
| failure report template | `templates/failure_report.md` | rendered per red validation run |
| worker persona | worker profile `SOUL.md` (identity incl. Minibook display name) | written by `provision-worker.ps1` |
| worker procedure | `skills/captain-worker/SKILL.md` (+ `scripts/`) — YAML frontmatter (`name`, `description`) + literal tool-call shapes | registered via `skills.external_dirs`; attached per cron job |
| cron prompt | one bland self-contained sentence inside `provision-worker.ps1` (create-time injection scanner rejects curl/token content) | per worker at provisioning |
| demo project description | `demo/project_description.md` | tested artifact, Day 3 |

### 20.4 Invocation map (end-to-end)

1. Operator runs `python -m agenten.pipeline.captain_pipeline` (own process).
2. Pipeline: stage 1 → 2 → 3 (+ deterministic checks + judge loop) → 4 per
   batch; each LLM call is a fresh `AssistantAgent` with structured output.
3. Stage 5 releases `work_batch` + `holdout_cases` blocks via
   `GatewayHTTPClient` → gateway persists + mirrors to Minibook.
4. Hermes cron fires (per worker): bland prompt + `skills: [captain-worker]`
   + `workdir` → agent claims a batch via `terminal(curl POST …/claim)`.
5. Worker step 3 renders `codex_task.md` from the bundle; step 4 launches
   codex in background; the completion notification wakes the agent.
6. Worker deploys (adapter, per-path §10), fetches holdout cases, validates
   (`scripts/validate.py` incl. soft-check judge), writes blocks via curl —
   every write fenced by the claim token.
7. Red → failure report → `codex exec resume` (≤ 3 iterations); green or
   exhausted → `batch_done` → gateway mirrors the outcome; Minibook thread
   shows the full build conversation.

### 20.5 Provenance of the BUILT agents (what the pipeline produces)

The built artifact (n8n workflow; later: agent teams via other adapters) gets
its prompts/tools/context through one auditable chain — no step is ad hoc:

| aspect | source | enforced by |
|---|---|---|
| system prompts (LLM nodes) | authored by Codex, derived from the context bundle's goal/constraints — Captain prescribes BEHAVIOR (acceptance criteria), not prompt text | validation loop: a prompt that misses a criterion gets a fix iteration; prompts live inside the workflow definition → referenced by the `deploy` block, auditable |
| user prompt / runtime input | the webhook payload ONLY (test: golden/holdout cases; prod: real leads) | context-bundle rule extends to the artifact: no hidden data sources; AGENTS.md forbids other inputs |
| tools (= n8n nodes) | node set constrained by the AGENTS.md contract: mail via `mailpit-smtp`, LLM via `openai-gpt` (GPT-5.6), HTTP only to the sink URL | credentials referenced by NAME, pre-provisioned Day 1; Codex may never create credentials or inline keys |
| context / memory | stateless per execution | persistence (e.g. lead history) would be an n8n data table — out of scope this week |

Net effect for the submission story: the pipeline is GPT-5.6 end to end —
GPT-5.6 plans (Captain), GPT-5.6 drives the build (Hermes workers), Codex
builds, and the built artifact is itself a GPT-5.6 autogen agent team that
ORCHESTRATES n8n tools (the two-layer architecture, §21 — autogen thinks, n8n
acts). n8n hosts no LLM logic; the reasoning lives in the built autogen team.

## 21. Layered architecture: autogen orchestrators over n8n tools

This is the load-bearing architecture the demo realizes. Two layers, cleanly
split (user directive):

- **Brain = autogen-core 0.7.5 agent teams.** All reasoning/orchestration lives
  here. Built by Codex against autogen-agentchat 0.7.5.
- **Hands = n8n workflows exposed as tool calls.** n8n provides its integration
  library as callable tools; it hosts NO agent/LLM logic ("only if absolutely
  necessary"). Built by Codex via the n8n MCP.

```
Task X → Captain decomposes →
   n8n-tool batches (target:"n8n")     autogen batch (target:"autogen")
   Codex builds workflows via n8n-MCP  Codex builds the team via autogen SDK
        │ exposed as tool calls               │ has the n8n tools wired in
        └──────────────►  orchestrated by  ◄──┘
                    the autogen team to solve Task X
```

**Two build targets, one seam.** The `target` enum (§6) now has two members:
`"n8n"` (build a workflow-tool) and `"autogen"` (build the orchestrator). Both
run the identical worker cycle (§8), ledger protocol (§4), claim fencing,
holdout isolation, and tree-rollup validation — only the adapter
(deploy/execute/observe) and the assertion subset differ.

**Dependency wiring (align becomes load-bearing).** The autogen orchestrator
batch DEPENDS ON its n8n-tool batches — the tools must exist and be validated
before the orchestrator is. The `interfaces`/dependency fields the align stage
already emits (§6) now drive real ordering: n8n-tool batches build and validate
first; the autogen batch's context bundle names the validated tools it may
wire. A worker will not claim the autogen batch until its dependency batches
show `batch_done: succeeded` (gateway check on claim).

### 21.1 n8n workflow-as-tool exposure

How a built n8n workflow reaches an autogen team as a callable tool — decided
at a Day-1 spike (mirrors the §9 Gate A go/no-go):
- **Primary: n8n MCP Server Trigger → autogen MCP workbench.** The n8n workflow
  is built with an MCP Server Trigger so n8n exposes it as an MCP tool;
  autogen consumes it via `autogen_ext.tools.mcp` (`mcp_server_tools` /
  `McpWorkbench` with `SseServerParams`/`StreamableHttpServerParams`). The
  agent calls the n8n workflow as a native tool.
- **Fallback: webhook-as-FunctionTool.** Wrap the workflow's webhook in a
  trusted `FunctionTool` (HTTP POST to `/hook/{batch_id}`) provided via
  `captain_tools.py`. Simpler, always works, no MCP dependency.
Either way the tool set handed to the built team is CONTRACT-CONSTRAINED (only
the validated n8n tools named in the bundle), never Codex-invented.

### 21.2 autogen build target (artifact, adapter, assertions)

**Artifact.** Codex authors `workspaces/{batch_id}/team.py` — a pure factory
`build_team() -> BaseGroupChat` (no import side effects), team `label =
captain-team-{batch_id}`. `team.json` (`team.dump_component().model_dump()`,
a `ComponentModel`) is the **diff/audit artifact of record** the `deploy` block
points at (hashed, secret-free).

**CRITICAL framework correction (verified against installed autogen 0.7.5 by
the review panel — three independent verifiers converged):** tool
serialization through `dump_component()`/`load_component()` is NOT reliably
supported in 0.7.5. Agent configs emit tools under `config.workbench`
(a `StaticStreamWorkbench`, whose `config.tools[].config` carry FunctionTool
`source_code`), NOT an agent-level `config.tools` list — and the official docs
note "serializing tools is not yet supported" (emit `"tools": []`). Consequence
for the design:
- **Execution substrate is `team.py`, not `team.json`.** The trusted
  `run_team.py` harness EXECUTES by importing `build_team()` in an isolated
  subprocess (tools are live Python objects) — it does NOT run a rehydrated
  `team.json`. `team.json` stays the audit/record artifact only.
- The deploy-gate schema reads tool provenance at
  `config.participants[i].config.workbench[j].config.tools[k].config.name`
  (and validates `provider==FunctionTool` + source against trusted
  `captain_tools.py`), never an agent-level tools list — else the gate finds
  zero tools and the provenance guarantee silently no-ops.
- **Day-1 spike proves this before the schema freezes:** dump a team with a
  FunctionTool, inspect where tools land and whether a round-trip executes
  them; also confirm `OpenAIChatCompletionClient` built without an api_key
  serializes `api_key: null` (not a placeholder or the env value).

**Adapter (deploy/execute/observe):**
- `deploy()`: assert `team.py` present; in a SUBPROCESS (never in-process —
  Codex code is untrusted), `build_team()` → `dump_component()` → validate
  against the `AutogenTeamConfig` Pydantic schema (component_type=team, model
  `gpt-5.6`, api_key null, no `sk-`, tools ⊆ trusted set at the workbench path
  above) → write `team.json`. Record team label, participants, tool names,
  model in the `deploy` block. Idempotent: one canonical `team.json` per batch.
- `execute(test_case)`: `terminal("python run_team.py --case case.json")` —
  `run_team.py` imports `build_team()` FRESH per case (teams are stateful
  across `.run()`), then `result = await asyncio.wait_for(team.run(task=...),
  CASE_TIMEOUT)`, dumps the observation to stdout as JSON. Stays inside the
  worker's terminal-only tool surface (§20.2); no in-process import of Codex
  code by the worker/gateway.
- `observe()`: off the `TaskResult` — `final_json` (last message parsed),
  `stop_reason`, `tool_calls` (from `ToolCallRequestEvent`/
  `ToolCallExecutionEvent`/`ToolCallSummaryMessage` in `result.messages`),
  `speakers` (ordered `source`), `message_count` — plus the shared
  `GET /sink/crm?case_id=` (§5). (Exact event classes/fields confirmed by one
  recorded run dumped to JSON, Day 3.)

**Assertion vocabulary (autogen subset, each bound 1:1 to an observation
channel — §7 rule):**
```
final_output{json_path_equals}        → final_json     (∥ webhook_response)
tool_called{name, args_json_path?}    → tool_calls      (∥ mail_sent)
tool_not_called{name}                 → tool_calls       (∥ no_mail)
termination_reason{value}             → stop_reason      (∥ execution_status)
speaker_participated{agent} / handoff → speakers         (autogen-native topology)
sink_called{json_path_equals}         → sink             (SHARED with n8n)
```
Correlation, holdout isolation, failure classification, thresholds, and
tree-rollup are all INHERITED unchanged from §4/§5/§7/§11. A team that won't
serialize/validate fails `deploy()` before any case runs.

**What of the old Captain build capability** (verified: it already targets
0.7.5 runtime objects, NOT pyautogen 0.2 — the only 0.2 vestige is the
`{"config_list":[...]}` shim in `_get_model_client`):
- MODERNIZE: drop the `config_list` shim → inject/serialize a
  `ChatCompletionClient` directly; the built team's model client becomes a
  Component sub-config `{provider: OpenAIChatCompletionClient, config:{model:
  "gpt-5.6"}}`. Keep `create_agent_assistant` as the Captain's INTERNAL helper
  for its own planning agents (it stops being the "build target").
- DROP from the build vocabulary: `make_system_prompt` as a deliverable
  (system prompts now live inside `team.py`, authored by Codex from the bundle,
  validated behaviorally); `create_agent_user_proxy` (no UserProxyAgent in
  0.7.5; runtime input is the task payload); the bespoke `register_tool`/
  `ToolRegistry` (never wired to `AssistantAgent(tools=)` anyway) → built-team
  tools are autogen-core `FunctionTool`s / MCP tools from the contract.

### 21.3 Stabilization via the Hermes learning loop

Hermes' built-in closed learning loop (skill creation from experience, skill
self-improvement during use, agent-curated memory, FTS5 cross-session recall)
is what makes the build FLEET improve over time — the "stabilizes over time"
property (user insight).
- **Mechanism:** across batch builds the worker accumulates skills ("how to
  build/validate an n8n Slack integration", "how to wire n8n MCP tools into a
  RoundRobinGroupChat", "what this Codex failure means") and memory (which
  prompts / node configs / team shapes pass) → fewer fix iterations, higher
  first-pass rate on similar later batches.
- **Measured by the ledger — not asserted:** iteration-count-to-green per batch
  is already recorded (`validation_run` count before `batch_done`). The
  stabilization is a plottable trend (iterations ↓, first-pass ↑ across similar
  batches) read straight from the ledger. This is the evidence layer for the
  "self-improving" claim.
- **Fleet learning:** shared skills dir via `skills.external_dirs` (all workers
  read one learned-skills location) → collective compounding; per-worker
  memory stays independent. Default: shared skills, separate memory.
- **Honest scope:** the mechanism is real and config-enabled; a 3-batch demo
  shows EARLY signal (a few similar batches, iteration count dropping), framed
  as the capability with the ledger as instrument — NOT a full learning curve.
  The video does not overclaim.
- **Reset interaction (critical):** the reset script (§14) must PRESERVE Hermes
  memory + the shared skills dir. It wipes work-products (ledger, n8n
  workflows, Mailpit), never the learned brain — else every "clean" run resets
  learning and nothing compounds.

### 21.4 Validated-capability reuse (Captain-level compounding)

The third compounding layer (user directive): passed tests aren't just history —
they are a queryable catalog the Captain reuses, so it rebuilds ONLY what is
missing or doesn't fit.

- **Every `batch_done: succeeded` is a validated capability:** goal + interface +
  the acceptance assertions it passed + an artifact reference (n8n workflow id or
  `team.json` hash). Recorded in the ledger (MariaDB) and mirrored to minibook's
  `/api/v1/registry` (whose fields — capabilities, tools_py_path/artifact ref,
  eval_score, status=validated — already fit).
- **Gateway read endpoint** `GET /capabilities?need=<descriptor>` searches the
  validated set over MariaDB (indexed query — cheap at any scale).
- **Captain enrich, per needed capability, queries the validated set:**
  - **fits** (semantic + interface match against the passed assertions) → REFERENCE
    the existing validated artifact; the batch is marked `satisfied_by: <ref>`,
    NO build dispatched.
  - **missing / mismatch** → emit a build `work_batch` (the normal Codex
    build-validate loop). This is the "retry if something is missing / doesn't
    fit" path; "fits → okay" is the reuse path.
- **Payoff:** over runs the validated-capability library grows; new tasks compose
  existing validated tools/agents; a task may need ZERO new n8n-tool builds and
  only the orchestrator wiring. Rebuilds shrink. This is PLANNING-level compounding,
  complementing the BUILD-level Hermes learning loop (§21.3) — the two stack. Both
  are measurable in the ledger (reuse-rate ↑, new-build count ↓).
- **Invariant:** Captain queries via the gateway read API and never writes the
  ledger except via release; the `satisfied_by` reference keeps the tree edge
  visible — resolved to an existing validated node instead of a fresh build subtree,
  so tree-rollup validation (§4) still holds.

## 22. Data stores

Most "state" is derived and lives in the ledger; only a few independent stores
exist.

| store | backend | who | notes |
|---|---|---|---|
| **Ledger** (authoritative write-model) | **MariaDB** | we operate | see decision below |
| Minibook (read-model) | SQLite | submodule, untouched | don't fork its DB init |
| n8n (workflows/executions/credentials) | SQLite volume | persist | n8n can't use MariaDB (MySQL/MariaDB support dropped — SQLite/Postgres only) |
| Hermes (memory/sessions/FTS5) | SQLite per profile | persist, **never wipe** | the learning substrate (§21.3) |
| Mailpit, gateway status index, mock-CRM sink | in-memory | — | ephemeral, reset per run |

Files, not DBs: shared skills dir, Codex sessions (`~/.codex/sessions/`), batch
workspaces, `team.py`/`team.json`, built n8n workflow exports.

**Decision: the ledger moves from JSON file to MariaDB.** Rationale beyond
durability — it DISSOLVES two review blockers by construction:
- Ledger corruption (the last-writer-wins snapshot / `os.replace` Windows race
  / silent genesis-wipe) → gone; transactional writes. The §5 lock-file and
  corrupt-rename mitigations become UNNECESSARY.
- Claim fencing race → an atomic transactional compare-and-set
  (`UPDATE ... WHERE status='pending'` / `SELECT ... FOR UPDATE`); MariaDB
  row-locking replaces the `asyncio.Lock` gymnastics. "Single-owner process"
  relaxes to "gateway is the sole WRITER"; readers connect directly.

Schema: a `blocks` table (`index` PK, `parent_index` FK, `block_type`, `data`
/`metadata` native JSON, `hash`, `previous_hash`, `created_at`); subtree/status
queries become indexed SQL. Implemented as a new `MariaDBStorage` behind the
existing pluggable `LedgerStorage` seam — no core change. Cost: +1 docker
service (`mariadb` image) + creds in `.env` + `PyMySQL` driver (pure-Python,
Windows-friendly) + MariaDB in the startup order (healthy → gateway). Honest
note: MariaDB is heavier than a demo strictly needs (SQLite/WAL would suffice),
but it buys definitively-correct concurrency and fits the "real system that
compounds" framing. Minibook stays on its own SQLite (submodule — not forked).

**Vector DB — NOT needed, and if ever, no new store.** The MVP runs on
structured queries + keyword search + LLM-reads-small-catalog; at demo scale an
LLM reads the whole validated-capability set / conversation slice in-context, so
embeddings add a pipeline for zero demo payoff. If semantic retrieval later
earns its place, MariaDB 11.8 LTS has NATIVE vector search (VECTOR type, HNSW
`VECTOR INDEX`, `VEC_DISTANCE_COSINE`, no extension) — embeddings live in the
same MariaDB we already run, no separate vector DB. First use when it earns it:
§21.4 semantic capability retrieval (embed capability descriptors → top-K +
LLM-judge fit) — the compounding critical path. Drawing insights from the
Minibook conversations (semantic failure clustering) is COULD-tier and
post-demo: the actionable version already exists via Hermes FTS5 session recall
(§21.3) + structured `validation_run` failure categories (plain SQL GROUP BY).
Embedding model would be OpenAI `text-embedding-3` (stays in the OpenAI stack).

## 23. Adapting the prepared minibook swarm pipeline

`minibook` already ships a working LLM-driven agent-team
FACTORY (`swarm/pipeline.py::SwarmPipeline`, driven by `autogen_swarm.py`): an
11-role swarm coordinating via Minibook forum posts, generating
autogen-agentchat team SOURCE with an LLM (`swarm/llm.py::call_gpt4o`, default
`claude-sonnet-4-6`; plus Claude Code CLI), scaffolding YAML + a static
`GENERIC_MAIN_PY` loader + `src/tools.py` + Dockerfile, building/running each
team in per-team Docker, scoring the live run with a single builder-visible LLM
verdict (`is_pass = verdict==PASS and score>=6`), and self-registering validated
teams into `/api/v1/registry` (HTTP 422 unless `eval_score>=6`).

**It independently reached our architecture's shape** — generate a validated
agent artifact, then register it, on autogen-agentchat `>=0.4` (NOT pyautogen
0.2). That is validation that the design is right. But it is a DIFFERENT
orchestrator with none of our invariants (no ledger, no claim fencing, no
holdout, no Codex, mutable status, forum coupling). **Adoption principle: keep
the registry tail, replace the whole head, and — per the review panel — write
the build gates fresh rather than salvage swarm code.**

- **GENUINE REUSE (narrow):** Minibook's `/api/v1/registry` + its `eval_score>=6`
  422 guard, kept running UNTOUCHED on Minibook SQLite (§22) — this IS the
  §21.4 validated-capability catalog. On `batch_done: succeeded` the **gateway**
  (not the built team, not a swarm agent) mirrors the validated batch via
  `swarm/api_client.py::register_agent_in_registry`. CRITICAL (verifier): that
  function is NOT a clean HTTP client — called with a `registry_agent_api_key`
  it ALSO fires forum join/post calls (the "head" we drop), and the endpoint is
  UNAUTHENTICATED, auto-creates an Agent, and mutates prior rows. So the gateway
  calls it with `registry_agent_api_key=None` (gates off the forum branch) OR
  vendors a trimmed HTTP-only POST; treats `/api/v1/registry` as a
  gateway-only writer; and only ever writes `status='validated'` derived from
  `batch_done:succeeded` (never the mutable `candidate` phase — that would
  contradict the append-only ledger story).
- **STEAL AS PATTERN (idea, not code):** the execute-artifact-then-read-output
  harness shape (`step_output_eval`); the bounded fix loop (`implement_todos`,
  max-2 retries) → our `codex exec resume` loop (max 3); the `ask_user`
  timeout-auto-approve discipline → our `--review` gate; the `docker info`
  preflight + build-timeout → our compose health-gating (§14).
- **WRITE FRESH — do NOT vendor (verifier correction):** the `team.py` deploy
  gate. `code_processing.py::test_generated_code` / `todo_implementer.py::
  validate_implementation` LOOK reusable but importing them drags
  `GENERIC_MAIN_PY`, and ~85% of their checks are dead against a single
  `team.py` factory (they hunt for `messages.py`/`host.py`/`worker.py`, a
  separate `tools.py`, distributed-grpc shapes; the "isolated import test" is
  gated on `find_file("messages.py")` and never runs). Vendoring-then-gutting
  is MORE work than a fresh ~30-line gate: `ast.parse` + `build_team() ->
  BaseGroupChat` presence + a real secret regex + `python -c "import team"` in
  a tempdir. Build it fresh against our clean seam.
- **DROP (conflicts with our invariants):** the Minibook-forum swarm
  orchestrator (replaced by Captain → ledger → Hermes → Codex); the in-pipe LLM
  builder `call_gpt4o`/`_call_claude_code` (the builder MUST be Codex — §2);
  per-team Docker + `docker_ops.py` (our autogen artifact runs via the
  `run_team.py` subprocess, §21.2); the YAML + `GENERIC_MAIN_PY` team format
  (we use `team.py` + `team.json` ComponentModel); the free-form LLM 1-10 score
  (replaced by the closed assertion enum + holdout, §7/§21.2 — the pipe has
  ZERO holdout, exactly the reward-hacking hole we close); and
  `GENERIC_MAIN_PY`'s runtime self-healing (`self_implement_tool`,
  `request_api_key`) which violates the secret-free, contract-constrained
  posture (§13).

**Open items before Day 2** (freeze with the gate schema): map the
gateway→registry payload fields (`tools_py_path`→workspace, `mcp_servers`→
validated n8n tool names, `run_id`, `output_dir`); confirm `src/main.py`'s
registry routes serve independently of `SwarmPipeline` (so we host the registry
without starting the forge runner); confirm no kept path transitively imports
`swarm/llm.py` (which would demand an Anthropic/OpenAI key at import time).

**Net:** the prepared pipeline gives us the registry (our capability catalog)
and confirms the architecture — but Captain + ledger + Hermes + Codex + the
assertion enum + holdout replace its head; only the registry service is real
reuse, and even it is called carefully to avoid dragging the forum coupling
back in.

## 24. Out of scope (this week)

Chain verification; recorder/CQRS integration for the new block types; any
Minibook write-back; Jira/code adapters (interface only); human-gate UI;
dynamic target selection by the LLM; packaging/`src/` migration. The `autogen`
target is IN scope as the second adapter proving generality, but its filmed
demo remains COULD — n8n stays the primary filmed demo unless the autogen
path proves solid by Gate C.

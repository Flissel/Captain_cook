# Captain → Hermes → Codex Pipeline — Design Spec

**Purpose:** OpenAI Build Week submission (deadline 2026-07-21 17:00 PT, category
Developer Tools) built as a real extension of Captain_cook: a pipeline where the
Captain decomposes a project description into work batches, a fleet of Hermes agents
builds each batch by driving Codex CLI, and the result is validated against
measurable outcomes — every step recorded in the append-only ledger.

**Status:** Approved design (brainstormed 2026-07-14/15, hardened by a 30-agent
adversarial review — 24 confirmed findings + 19 notes, all folded in).

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

- **Baseline tag:** verify the official cutoff wording in the rules, then commit the
  current tree and tag it `pre-codex-baseline`. The tag — not a date — is the sole
  prior-work boundary. `PRIOR_WORK.md` lists what predates the hackathon work
  (incl. decompose/judge from cf50e87) and discloses honestly that the foundation
  was built with Claude Code on 13–14.07.
- **Embedded repos:** `hermes-agent/` and `Autogen_AgentFarm/` are currently bare
  gitlinks with no `.gitmodules` — a judge cloning the repo gets EMPTY dirs. Fix:
  proper submodules with pinned SHAs (`git rm --cached` + `git submodule add`;
  hermes pinned to 77d5b2d; Autogen_AgentFarm is pushed to github.com/Flissel).
  Smoke test: `git clone --recursive` into a temp dir and confirm both dirs
  are non-empty — the fix isn't done until the clone proves it.
  Minibook stays behind the submodule pointer → its AGPL-3.0 stays isolated
  (HTTP-only integration, no linking); one README sentence notes this.
- **Codex session capture (dev-time):** a shell wrapper logs every dev
  `codex exec` session ID + date + intent to `docs/codex-sessions.md`; commits
  carry a `Codex-Session: <id>` trailer. Recovery: harvest `~/.codex/sessions/`
  rollout files. README leads with dev sessions; runtime thread_ids are the
  novelty layer on top. One honest sentence covers Claude Code's planning role.
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
| `work_batch` | Captain | context bundle: goal, subtask ids, constraints, interfaces, `target` (enum, only `"n8n"` this week), acceptance criteria (assertion enum), **build-visible** golden cases |
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

### Claim fencing (review finding: double-execution was guaranteed without this)

- `POST /batches/{id}/claim` returns a **claim_token**; initial expiry **90 min**.
- Every child-block write must carry the current token → else **409**.
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
GET  /batches?status=pending            list claimable batches
POST /batches/{id}/claim                → claim_token | 409
POST /batches/{id}/claim/heartbeat      extend expiry
POST /batches/{id}/approve              pending_review → pending (flag-gated)
POST /blocks                            schema-validated write (fenced by token)
GET  /batches/{id}/blocks               batch subtree
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

## 10. n8n + Mailpit deployment

- docker-compose: n8n pinned 2.29.x + Mailpit only (Minibook runs natively —
  no Dockerfile exists and none will be written this week).
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

1. Adapter `deploy()` (upsert) → publish gate passed.
2. For each holdout case (sequential): `execute()` = POST payload (with
   `case_id`, per-case recipient) to `/hook/{batch_id}` webhook.
3. `observe()`: webhook response fields; executions API (correlated by
   `case_id`); Mailpit search (`to:` per-case recipient); mock-CRM sink query.
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
  → post; claim/codex_task (prompt summary)/codex_session/deploy/
  validation_run/batch_done → comments by the acting agent's account with
  @mentions; `holdout_cases` is NOT mirrored (exam stays private). Posts
  reference block **index + type + stable hash**.
- Demo footage films the forum of a COMPLETED run (never live).

## 13. Secrets

| secret | lives in | must never reach |
|---|---|---|
| `OPENAI_API_KEY` (GPT-5.6 Captain) | `.env` → Captain process env | ledger, Minibook, codex workspace |
| n8n API key | `.env` → gateway + adapter env | ledger, Minibook, codex workspace, cron prompts |
| n8n MCP access token (`N8N_MCP_TOKEN`) | codex env (global MCP config) | ledger, Minibook, cron prompts |
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

**Reset script** (Day 2, ~20 lines): stop workers → archive `blockchain.json`
to `runs/<ts>/` → start fresh → delete all n8n workflows via API → wipe Mailpit
(`DELETE /api/v1/messages`) → optionally wipe `minibook.db`. Run before every
E2E and before filming. Never touches the n8n docker volume.

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

- **Day 1 (Wed 15., today):** credits requested; model→gpt-5.6 + smoke test; baseline
  tag + PRIOR_WORK.md + submodule fix; docker-compose up + n8n bootstrap +
  `mailpit-smtp` and `openai-gpt` credentials; **GATE A (binary, scripted):** fresh workspace →
  codex exec builds the trivial workflow (MCP path; n8n tool names visible in
  the JSON event stream) → deployed → webhook POST → mail visible in Mailpit —
  **twice in a row**. MCP experiments timeboxed to the morning, 15:00 go/no-go
  → else switch to the JSON+REST fallback path (Gate A also exercises the
  REST deploy once via curl so the fallback is proven, not assumed). Verify
  `codex exec resume` + a 12-min codex run under hermes cron (background
  mode). Confirm ChatGPT-plan rate limits cover ~50–100 exec turns for the
  week, else flip codex auth to the API key. Hermes single instance installed
  in parallel.
- **Day 2 (Thu 16.):** gateway (single-owner enforcement, fencing, sink,
  schemas, tests) — direct `Blockchain` writes, `async def` + lock; pin
  fastapi/uvicorn in requirements.txt; hash stability fix; LedgerClient seam
  in Captain; assertion-enum module frozen; reset script.
- **Day 3 (Fri 17.):** ⚠ credits deadline 12:00 PT. captain-worker skill + n8n adapter on ONE hermes
  instance; Captain align/enrich + pipeline driver; demo description tested →
  3 batches; write `provision-worker.ps1` (used Day 4). **GATE B:**
  single-worker E2E green by evening — else fleet is cut to 1 worker +
  narrated architecture (story mode).
- **Day 4 (Sat 18.):** fleet ×3 via provision-worker.ps1; Minibook mirror +
  account provisioning; hardening (heartbeats, expiry warnings, terminal-state
  rejects).
- **Day 5 (Sun 19.):** footage-capture runs (codex terminal, Minibook, n8n
  canvas, Mailpit) — screen-record everything; README (English, quickstart for
  judges, session table, tooling honesty, AGPL note, license section);
  repo cleanup (`test_claude_output.txt` out, plan docs → `docs/planning/`,
  real title); video script written in the evening — leads with the
  "Captain writes the spec AND the exam" loop; cut from the script:
  hash-chain internals, CQRS, recovery, fleet mechanics (README screenshots
  instead). Forum footage is always of a COMPLETED run.
- **Day 6 (Mon 20.):** edit + upload video (<3 min, YouTube public); Devpost
  form draft complete.
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

- [ ] Category: Developer Tools; project name matches README title
- [ ] Text description (English): features + how Codex and GPT-5.6 were used
- [ ] Video: <3 min, public YouTube, clear audio, shows the built project
- [ ] Repo public; README quickstart lets judges run it (compose + env template
      + one command per component); testing access instructions
- [ ] Codex Session IDs: dev-session table in README + `docs/codex-sessions.md`
      + commit trailers; runtime thread_ids shown as ledger blocks
- [ ] PRIOR_WORK.md + `pre-codex-baseline` tag referenced in README
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
GPT-5.6 plans (Captain), GPT-5.6 orchestrates (Hermes workers), Codex builds,
and the built artifact itself runs its LLM nodes on GPT-5.6.

## 21. Out of scope (this week)

Chain verification; recorder/CQRS integration for the new block types; any
Minibook write-back; Jira/code adapters (interface only); human-gate UI;
dynamic target selection by the LLM; packaging/`src/` migration.

# Anthropic switch, OpenClaw Captain, ledger hardening

Date: 2026-11-07. Base: `main` @ `dbe244f`. Supersedes the Nebius/Nemotron
hackathon direction (`docs/hackathon/NEBIUS_SUBMISSION.md` is no longer the
target).

## Decisions

- **Billing:** Claude subscription, consumed only through the official Claude
  Code CLI. Captain components (planner stages, judge, Hermes, AutoGen teams)
  reach it through `scripts/claude_code_openai_shim.py`; OpenClaw uses its
  bundled `claude-cli` backend. Never extract or forward OAuth tokens from
  Claude Code into other clients.
- **API-key fallback stays switchable:** a native `anthropic` provider with
  `ANTHROPIC_API_KEY` must be selectable by env alone, so a subscription
  policy change costs one config line, not a code change.
- **Orchestrator:** OpenClaw in a container replaces the LLM-driven planning
  of `CaptainPipeline`. Deterministic gates (alignment validation, planning
  policy, `CanonicalPlanCompiler`, constitution rule layer, release/approval
  gates) stay in Captain code and are exposed to OpenClaw as MCP tools.

Risk: Anthropic's subscription terms for third-party harnesses were tightened
in 2026 (reports of OpenClaw OAuth blocks in April 2026). OpenClaw's docs treat
the `claude-cli` backend as sanctioned. Re-check Anthropic's current terms
before production use; the API-key fallback is the mitigation.

## Audit findings that drive the order

Captain is not an AutoGen agent. `CaptainPipeline`
(`agenten/planning/captain_pipeline.py:121`) is deterministic except four LLM
stages: decompose, align, enrich (`agenten/planning/factory.py:42-105`) and
the constitution layer-2 judge (`agenten/constitution/gatekeeper.py`). AutoGen
is used as an HTTP client and for benchmark graph teams.

The ledger is a sound base for Captain-orchestrated claim/lease work (locked
hash-chained appends, derived batch state, claim/fence/recovery, idempotent
replay) but not yet a trust base for generated agents.

## Phase 0 - land pending fixes

- [ ] Merge PR 33 (`fix/gateway-lease-recovery`): lease recovery per claim
      iteration, heartbeat extends claim, review gate, CI regressions,
      benchmark broker operation check.

## Phase 1 - ledger must-fix (before any agent gets tools)

- [x] `POST /blocks` allow-list instead of deny-list. Today only
      `GATEWAY_OWNED_EVENT_TYPES` (5 types, `gateway/store.py:191`, check at
      `:7778`) are rejected, so a worker with a live claim can write
      `delivery_event`, `agent_runtime_command`, `agent_runtime_grant`, ...
      Workers may write only `codex_session`, `codex_process`,
      `reasoning_slice`, `validation_run`, `batch_done`; Captain only
      `problem`, `work_batch`, `holdout`; everything else via dedicated routes.
- [x] Tamper evidence (done: hashed `batch_review_required` child instead of a
      `WorkBatch` field, startup `verify_ledger_chain`, append-only DB triggers
      instead of grants because MariaDB cannot partially revoke a database-wide
      grant; batches created before the change keep the old status rule): `project_batch` starts from unhashed `parent["status"]`
      (`gateway/contracts.py:1090`). Hash `status` (and schema metadata) or
      derive the start status from hashed data; run `verify_chain` at gateway
      startup and periodically (today it has no production caller); give the
      gateway DB user INSERT/SELECT only on `blocks`.
- [x] Agent identity (done: `WORKER_AGENT_TOKEN_SHA256_JSON` per-agent tokens,
      claims bound via `ClaimEvent.agent_id`; heartbeat, evidence, holdout and
      delivery events refused for other agents; `GATEWAY_REQUIRE_AGENT_IDENTITY`
      refuses shared-token claims. Also closed: any worker could post delivery
      events against another claim because `claim_id` is world-readable): claims carry no `worker_id` and all workers share one
      token (`gateway/auth.py:60-80`, claim route `gateway/app.py:1567`).
      Per-agent tokens, `agent_id` in `ClaimEvent`, checked against
      `trace.worker_id`.
- [x] `POST /v1/runtime/results` (`gateway/app.py:1479`) accepts any role;
      bind results to the agent that holds the grant. (done: optional
      `CapabilityGrant.holder_agent_id`, must be a registered agent at grant
      time; a bound grant's result is accepted only from that agent's token,
      not Captain or the shared worker token; unbound grants keep role-only
      checks, but anonymous worker results are refused once
      `GATEWAY_REQUIRE_AGENT_IDENTITY` is on. Gateway-internal services
      record results without the reporter check. Nothing sets the holder
      yet; Phase 2 `agent_assigned` / grant issuance must fill it.)
- [x] Captain-initiated batch cancel/revoke block, signalled to the claim
      holder. (done: gateway-owned `batch_cancelled` via Captain-only
      `POST /batches/{id}/cancel` with a reason; terminal from
      `pending_review`, `pending` (also an expired, unrecovered claim) and
      `claimed`; revokes the claim; replay with the same reason is 200.
      The holder gets 410 "batch cancelled" on heartbeat, evidence, holdout
      and delivery events; the delivery client raises
      `GatewayBatchCancelledError`, a subclass of the conflict error. Still
      open: Captain must terminalize an active Codex session after a cancel,
      as after recovery; no worker loop consumes heartbeat 410 yet.)
- [x] `JSONFileStorage.load` returns `[]` on corrupt JSON
      (`blockchain/storage.py:41`) - fail closed instead. (done: raises
      `LedgerStorageCorruptError` for invalid JSON, a non-list root or
      non-object entries, and leaves the file untouched; before, `Blockchain`
      wrote a fresh genesis block over the damaged file. `reset=True` still
      replaces it on purpose. Reads and writes are UTF-8.)
- Moved to Phase 2: unify the two `batch_done` schemas. Phase 2's
  `agent_outcome_recorded` reshapes the same outcome and release-policy code,
  so doing it now would touch that code twice.

## Phase 2 - agent lifecycle contracts

Design calls (2026-11-09, made without a design round at the user's
request): agent records live in the gateway ledger as Captain-only,
gateway-owned block types with dedicated routes; bearer tokens stay in
`WORKER_AGENT_TOKEN_SHA256_JSON` (authentication), the ledger adds the
lifecycle (authorization). Order is by risk and independence:

- [x] Scope release-policy evidence to the e2e run's claim
      (`batch_id`, `claim_id`, `fencing_token`); a retried batch can no
      longer borrow a deploy or validation from another claim iteration
      (`gateway/release_policy.py`).
- [x] Delivery-event `batch_done` (`gateway/contracts.py:540`) has no
      producer or consumer and a different outcome vocabulary than the
      lifecycle `batch_done` (`:944`). Refuse new delivery `batch_done`
      appends; keep stored ones readable. The lifecycle block is the only
      terminal record.
- [x] Dead letter: `batch_cancelled` with the new reason `unroutable` (no new
      terminal status, so status filters and projectors stay unchanged). Was:
      `batch_dead_lettered`: Captain route marks a non-terminal batch as
      permanently unroutable (reason, detail), terminal status
      `dead_lettered`, replay-idempotent like `batch_cancelled`.
- [x] `agent_registered` (agent_id, version, owner, spec_sha256) and
      `agent_retired`; `project_agent` (`gateway/agent_lifecycle.py`). A
      retired agent's token loses all write routes (403); reads stay open.
      Routes: `POST /agents`, `POST /agents/{id}/retire`, `GET /agents/{id}`.
- [x] `agent_capability_granted` / `agent_capability_revoked` (grant_id,
      capability tags, expiry). A ledger-registered agent may claim only a
      batch whose `capability_tags` one active grant covers (grants are not
      unioned). The runtime `CapabilityGrant` (per command, profile-fixed)
      stays separate. Routes: `POST /agents/{id}/grants`,
      `POST /agents/{id}/grants/{grant_id}/revoke`.
- [ ] Agents with a token but no ledger registration still claim anything
      (compatibility). Add a setting that requires ledger registration for
      every claiming agent once Captain registers all of them.
- [x] `agent_assigned` (batch, agent), stored as the batch lifecycle child
      `batch_assigned`: Captain pins a batch's claims to one agent
      (`POST /batches/{id}/assign`); other agents and shared-token workers
      get 403. Latest assignment wins; reassigning needs an unclaimed batch,
      never a retired agent. Projection field `assigned_agent_id`.
- [x] Agent outcomes: derived read model over batch projections, not a
      second written record, so there is no dual write to keep consistent.
      `GET /agents/{id}/outcomes` lists each claim (batch, fencing token) with
      its terminal status, `superseded`, `released` or `active`.
- [ ] Agent liveness independent of a batch claim: deferred. A 20 s
      heartbeat does not belong in the hash chain; needs a non-ledger store.

## Phase 3 - Anthropic as default provider

Branch `feat/anthropic-default-provider` (stacked on PR 34). Operator
decisions 2026-11-09: tool-using roles stay on the subscription (the shim
gains tool calling), the provider proxy is retired, live gates allowed.

- [x] `config/llm_provider.py`: `claude-code` (shim, subscription, default,
      tool calling) and `anthropic` (API key) providers. `claude-code` reads
      no API key and rejects model ids the shim would silently map to the
      account default. `nebius`/`openai`/`custom` stay on request.
- [x] `agenten/llm/model_client.py`: `anthropic` builds
      `AnthropicChatCompletionClient` with declared capabilities
      (`anthropic_model_info`); `autogen-ext[openai,anthropic]==0.7.5`
      (adds only `anthropic`, `docstring-parser`, `httpx2`, `httpcore2`,
      `truststore`). AutoGen 0.7.5's Anthropic client has no typed
      structured output, so that profile declares `structured_output=False`.
- [x] `agenten/evaluation/cli.py`: non-OpenAI providers go through the
      shared builder.
- [x] `agenten/agent_factory/benchmark_providers.py`: `anthropic` added.
      `claude-code` stays out: benchmarks need a per-provider key secret and
      a per-call cost ceiling.
- [x] Hermes: `hermes_provider_settings` defaults unconfigured Hermes to
      provider `custom`, model `claude-code`, shim URL; `anthropic` forwards
      `ANTHROPIC_API_KEY` to the child only. `parse_hermes_usage` needed no
      change: it records Hermes' own cost and `cost_status`.
- [x] Shim limits: the shim supports OpenAI tool calling (`--tools ""`,
      `--json-schema` envelope pinning each function name and parameter
      schema, `tool_calls` responses and streaming). Live-verified with
      AutoGen's OpenAI client: call, execute, answer
      (`tests/live/test_live_claude_code_shim_tools.py`).
- [x] Provider proxy: retired from the Anthropic path, not ported. It stays
      only as the Codex/OpenAI live-demo budget proxy (package docstring,
      `.env.example`, `docs/architecture/06-coding-execution.md`).
- [x] Tests pinning the Nebius default updated.
- [x] `.env.example`, README: Claude subscription default, provider table,
      migration note.
- [x] `scripts/live-demo-services.ps1` forwards and redacts
      `ANTHROPIC_API_KEY` next to `NEBIUS_API_KEY` (local allow-list,
      secret redaction, both runtime-adapter lists).
- [ ] Delete `agenten/provider_proxy/` once the live demo no longer runs
      Codex on OpenAI.
- [x] Shim structured output: prompt-only JSON came back in Markdown code
      fences, so `main.py demo --householder-executor llm` stalled after two
      failed householder attempts. A `json_schema` response format now runs
      through `--json-schema` and the shim returns the validated object;
      `json_object` strips a surrounding fence. Live: the LLM demo completes
      on the subscription (4/4 subproblems done, `claude-code-haiku`).
- [x] Check a Pydantic schema with `$defs`/`$ref` against the CLI's
      `--json-schema`: live test with nested models passes
      (`tests/live/test_live_claude_code_shim_tools.py`).
- [x] The event-driven demo waits forever when a householder fails
      permanently. Three causes, each fixed with a test:
      the first failure opened the agent type's circuit (1/1 > 0.5, no
      minimum sample count); the coordinator cached "open" and deferred
      every 30 s forever, so no outcome ever reached the supervisor's lazy
      half-open check; and an escalated block stayed `RETRYING` for good.
      Now the circuit needs `circuit_min_samples` (5) outcomes before it
      opens, `CircuitStateChanged(open)` carries `probe_after_seconds` and
      the coordinator lets exactly one probe through after it, and the
      recorder closes an escalated block as `FAILED` (`escalated=True`).
      The offline demo with an always-failing replay client ends after
      about 28 s with `success=False` and 4 failed blocks.
- [x] Retry delays slept inline in the publish chain (`InMemoryEventBus`
      runs handlers nested inside the recorder's writer loop), so ledger
      writes waited for backoff sleeps and the 4 failing householders above
      took 4 x 7 s serially. The coordinator now schedules a positive delay
      as a background task (`pending_retry_count`,
      `wait_for_pending_retries`, `cancel_pending_retries`; the pipeline's
      `stop` cancels waiting retries). The same offline demo ends after
      about 7 s instead of 28 s.
- [x] Worker execution ran nested in the publish chain: a
      `SubproblemAssigned` handler awaited the whole `execute()` inside the
      recorder's writer loop, so subproblems ran one after another and
      ledger writes waited for them. `build_pipeline` now subscribes workers
      through `BackgroundTasks.detach` (`agenten/runtime/background.py`,
      also used for the coordinator's delayed retries); `stop()` waits until
      workers and the ledger queue are idle. Live LLM demo: all four
      householders are assigned within about 4 s of each other.
- [x] Demo LLM call timeout: 30 s (sized for Nebius) cut off householder
      calls through the subscription shim, which take 14-25 s and sometimes
      over 30 s; live runs escalated whole subproblems. Now 90 s, and the
      convergence limit covers one householder exhausting every executor
      attempt on every supervisor retry (757 s worst case). Two live runs
      afterwards: 4/4 done, no retries, 60 s and 80 s.
- [ ] `run_llm_stage` does not log an attempt that timed out or failed
      before the next one, so a slow householder (76 s in one live run)
      cannot be told apart from a retried one. Log each failed attempt.
- [ ] Shim hardening: `--tools ""` is set only for tool requests; the
      plain-text path relies on non-interactive permission denials.

## Phase 4 - Captain MCP server

Thin MCP server over the gateway (bearer token per agent identity from
Phase 1). No generic `/blocks` write tool.

- [x] Read tools: `GET /batches`, `/batches/{id}`, `/batches/{id}/blocks`,
      `/batches/{id}/bundle`, `/capabilities?need=`,
      `/v1/runtime/operations/{id}`, run events, release status,
      `/v1/factory/jobs/{id}`. Done (2026-11-09): `list_batches`,
      `get_batch`, `get_batch_blocks` (holdout blocks never included,
      claim digests stripped), `get_agent`, `get_agent_outcomes`,
      `get_release_status`, `get_runtime_operation`, `get_factory_job`
      (UUID-validated ids), all with the agent token. Not offered: the
      bundle (duplicates blocks), run events (bulk evidence; add when a
      planner flow needs them), capabilities (used inside `compile_plan`).
- [x] Planning tool: `compile_plan` (+ `publish_plan`). Done
      (2026-11-09): the planner supplies decompose/align/enrich results
      (`SuppliedPlan`, `agenten/planning/supplied_plan.py`); they run as
      fixed stages through the unchanged `CaptainPipeline` with one
      alignment attempt. `compile_plan` is read-only; `publish_plan` needs
      the Captain token, recompiles instead of trusting a compiled plan,
      and releases every batch explicitly as `pending_review`
      (`GatewayPlanningClient(hold_for_review=True)`), so the operator
      approves even when the gateway's default gate is off. Results show
      holdout case counts only. Enabled by `CAPTAIN_PLAN_TARGET` and
      `CAPTAIN_PLAN_CAPABILITY_TAGS` (optional
      `CAPTAIN_PLAN_ALLOWED_TARGETS`).
- [ ] `publish_plan` is not resumable: after a partial release a retry
      fails with 409 on the first published batch. Route it through the
      `CaptainRunStore` checkpoint (`compile_and_release`); skipping
      "identical" batches is not safe because holdouts are not readable
      for comparison. Mitigated (2026-11-09): the error names the batches
      released before the failure.
- [ ] Planner-published batches need `GATEWAY_APPROVAL_ENABLED=true`;
      otherwise `/approve` is disabled and they stay held. Surface that as
      a startup check or a gateway capability flag.
- [x] Write tools: approve, recovery, review, runtime commands/grants/
      revocations, factory jobs, release decision, cancel (Phase 1).
      Done (2026-11-09), opt-in via `CAPTAIN_MCP_CAPTAIN_TOKEN`, all
      annotated: `register_agent`, `retire_agent`, `grant_agent_capability`,
      `revoke_agent_capability`, `assign_batch`, `cancel_batch`.
      `grant_agent_capability` is annotated destructive (it widens
      permissions). Approval is deliberately not a tool: it is the human
      review gate and stays with the operator (security review
      2026-11-09). Added: `recover_expired_claim` (destructive; the
      gateway accepts it only for a really expired claim) and
      `record_release_decision` (destructive; registered only with
      `CAPTAIN_RELEASE_POLICY_VERSION`, which pins the policy version; the
      gateway computes accepted/rejected fail-closed). Deliberately not
      offered: review decisions (release evidence; the planner would
      certify its own work), runtime commands/grants/revocations and
      Factory job creation (execution and budget authority stays with
      Captain's deterministic runtime).
- [x] Constitution check tool: HTTP/MCP wrapper around the rule layer of
      `ConstitutionGatekeeper` (today event-bus internal only). Done
      (2026-11-09): `check_constitution` (read-only) runs the deterministic
      validators over every subtask of a supplied plan
      (`agenten/constitution/plan_check.py`: required fields, minimality
      against the project description, in-plan duplicates, total
      budget); `publish_plan` refuses plans it rejects.
- [ ] Semantic constitution layer (LLM judge) for supplied plans. Not run
      by the planner on its own plan; needs an independent judge (a
      separate Claude call or the operator). Reports say
      `semantic_review: not_performed` until then.

## Phase 5 - OpenClaw container

- [x] Compose service `captain-openclaw` (Node + OpenClaw + Claude Code CLI),
      persisted home volume; user logs Claude Code in once inside the
      container. Validate compose config before starting.
      (`deploy/captain-openclaw/`, spike 2026-11-09.)
- [x] `openclaw.json`: `mcp.servers.captain` -> Phase 4 server; model via
      bundled `claude-cli` backend.

Spike result 2026-11-09 (OpenClaw 2026.10.4, Claude Code 2.1.319, read-only
MCP server with `list_batches`/`get_batch`): one `agent --agent main` turn
listed pending batches and read `spike-batch-1` from the throwaway ledger
through the subscription-backed `claude-cli` runtime
(`anthropic/claude-opus-5-5`). Findings:

- [x] The CLI container must share the gateway's PID namespace
      (`pid: service:...`); OpenClaw refuses state access from a foreign
      namespace (`GatewayLockNamespaceError`). Fixed in the spike compose.
- [ ] `models auth login` needs a TTY; no non-interactive path for the
      `cli` method. Provisioning stays an operator step.
- [ ] `mcp doctor` warns that `headers.Authorization` holds a literal
      secret although it is `Bearer ${CAPTAIN_MCP_TOKEN}`; check whether a
      whole-value env reference silences it.
- [x] `memory-core` creates a "dreaming" cron job on boot; decide whether
      the Captain planner container disables it (unplanned LLM turns).
      Decided: off (`plugins.entries.memory-core.config.dreaming.enabled:
      false` in the seed config). Live confirmation pending.
- [ ] The container also reaches the Captain gateway on the host (8090);
      it holds no gateway token, but the production shape should network-
      isolate the planner to the MCP server only.
- [ ] Gateway container uses about 1.3 GiB RAM idle.
- [x] Captain role prompt/skill: plan -> constitution check -> `compile_plan`
      -> `publish_plan` -> (operator approves) -> monitor -> release
      decision; never writes raw blocks. Done (2026-11-09):
      `deploy/captain-openclaw/workspace/AGENTS.md` (role, hard rules,
      confirmation before every ledger write, holdout secrecy) and the
      `captain-plan` skill (plan format, enforced rules, procedure), seeded
      on every start; drift tests in `tests/captain_mcp/test_openclaw_role.py`
      (confirmation list == write tools, skill example compiles). Seed
      config denies `group:runtime` and `group:automation`.
- [x] Live check of the role on OpenClaw 2026.10.4: `openclaw doctor`
      accepts the new config keys, the skill is listed, `exec` is denied,
      and one planner turn runs check -> compile without publishing.
      Done (2026-11-09, throwaway ledger, MCP without Captain token):
      `config validate` accepts `skipBootstrap`, dreaming off, `tools.allow`
      and `tools.deny`; `skills list` shows `captain-plan` ready
      (`openclaw-workspace`); two planner turns drafted 3-batch plans,
      passed `check_constitution` first time, compiled, showed holdouts as
      counts only and did not publish. Findings, all fixed:
  - [x] `tools.deny` alone left 44 OpenClaw tools in the API request
        (`terminal`, `computer`, `browser`, `file_write`,
        `sessions_spawn`, `message`, ...). Now an allow-list:
        `tools.allow: ["*captain*", "read"]`; the next request carried 0
        OpenClaw tools.
  - [x] Claude Code's native tools are not governed by `openclaw.json`:
        a probe created a file with native `Write`, and `Agent` started
        a subagent (it had no `Bash`). Fixed with Captain-owned
        `~/.claude/settings.json` `permissions.deny` (OpenClaw launches
        Claude with `--setting-sources user`); re-probe: `Write`, `Edit`,
        `Agent`, `Workflow`, `RemoteTrigger`, `Cron*`, `SendMessage`,
        `NotebookEdit`, `WebSearch` gone from the request, no file written,
        Captain tools still work.
  - [x] The spike volume kept OpenClaw's default `BOOTSTRAP.md`,
        `SOUL.md`, `IDENTITY.md` persona templates in the prompt. The seed
        now writes Captain `SOUL.md`/`IDENTITY.md` and removes
        `BOOTSTRAP.md`.
  - [x] Model self-reports about its tools are unreliable (it first
        over-, then under-reported). Verify tool exposure from the Claude
        transcript (`api-request-shape`, `deferred_tools_delta` records),
        never from the answer.
- [ ] `openclaw.json` is copied once; an existing volume needs the planner
      keys applied by hand (`openclaw config set ...`, done for the spike
      volume). Consider a seed step that merges Captain-owned keys.
- [x] Remaining native tools: `Read`/`Glob`/`Grep` (needed for skills) can
      read the whole container home, including `~/.claude/.credentials.json`
      and `openclaw.json`. Scope reads to the workspace (Claude permission
      rules such as `Read(~/.claude/**)` deny, or `tools.fs.workspaceOnly`)
      and verify with a probe. Done (2026-11-09, security review): `Read(...)`
      denials for `~/.claude`, `~/.local`, OpenClaw config/keys/state/agents/
      cache/media, `/proc`, `/etc`, `/app`. Probed with the container's
      `claude --setting-sources user` on harmless files (the planner itself
      refused the probe as a rules-suspension attempt, which is the intended
      role behaviour): workspace Read/Grep allowed; Read, Grep and Glob on
      `~/.claude`, `/proc/self/status`, `openclaw.json` denied by rule.
- [x] Name-based read denials miss new paths (security review). Replaced
      with structural default-deny: workspace moved to the non-hidden
      `/home/node/workspace` (`OPENCLAW_WORKSPACE_DIR`), `Read(~/.*)` and
      `Read(~/.*/**)` seal every hidden home path, `Read(//<root>/**)` every
      image root directory except `/home` and `/tmp`, and
      `Read(//tmp/openclaw-cli-mcp-*/**)` the per-turn MCP config holding
      the bearer header. Live probe 2026-11-09 from the new workspace: Read
      `IDENTITY.md` and Grep in the workspace allowed; Read `~/.bashrc`,
      `~/.claude.json`, `~/.openclaw/openclaw.json`, a `/tmp/openclaw-cli-mcp-*`
      dummy, `/etc/hostname` and Grep in `~/.openclaw` denied by rule;
      `skills list` shows `captain-plan` ready from the new workspace.
      Follow-up review: `/tmp/openclaw/` (OpenClaw log, per-turn skills
      plugin dir) was still readable; now all of `Read(//tmp/**)` is denied.
      A catch-all `Read(//**)` is not usable because deny beats allow and
      would seal the workspace too. Re-probed: the `/tmp/openclaw` log and
      Grep there denied, workspace readable, and an `openclaw agent` turn
      still loads `captain-plan` through the Skill tool.
- [ ] Recursive Grep/Glob from a parent such as `/home/node` is blocked in
      headless Claude only by the outside-cwd permission prompt, not by a
      rule (hidden subtrees are denied, `/home/node` itself is not). In
      OpenClaw turns that prompt goes to OpenClaw's permission tool; verify
      it refuses (or add a rule-level guard) before the planner
      runs unattended.
- [ ] Offline deterministic path stays (replay clients) so the test gate
      needs no subscription.

## Phase 6 - retire LLM planning stages

- [ ] Keep decompose/align/enrich as optional fallback; OpenClaw becomes the
      default planner once Phase 5 has an end-to-end run recorded in the
      ledger.

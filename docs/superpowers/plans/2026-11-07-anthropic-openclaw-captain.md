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

- [ ] `config/llm_provider.py`: add `claude-code` (shim, subscription) and
      `anthropic` (API key) providers; make `claude-code` the default instead
      of `nebius`. Keep `nebius`/`openai` optional.
- [ ] `agenten/llm/model_client.py`: `anthropic` builds
      `AnthropicChatCompletionClient`; add `autogen-ext[anthropic]` pinned to
      the existing autogen version in `requirements.txt` (verify the extra).
- [ ] `agenten/evaluation/cli.py:209-233`: reuse the shared client builder.
- [ ] `agenten/agent_factory/benchmark_providers.py`: extend
      `BENCHMARK_PROVIDERS`.
- [ ] Hermes: default `CAPTAIN_HERMES_PROVIDER=custom` +
      `CAPTAIN_HERMES_BASE_URL=http://127.0.0.1:8114/v1`,
      `CAPTAIN_HERMES_MODEL=claude-code`; API-key path forwards
      `ANTHROPIC_API_KEY` to the child (`hermes_endpoint.py`); teach
      `parse_hermes_usage` Claude/shim pricing (subscription = 0 marginal
      cost, but keep call/time budgets).
- [ ] Shim limits: `custom` sets `tool_calling=False`; decide whether the shim
      gains tool calling or tool-using roles move to the API-key path.
- [ ] Provider proxy (`agenten/provider_proxy/`) is pinned to OpenAI
      `/v1/responses`: retire or port.
- [ ] Update the 14 test files that pin Nebius/Nemotron defaults
      (`tests/test_llm_provider.py`, `tests/test_llm_config.py`,
      `tests/llm/test_model_client.py`,
      `tests/agent_factory/test_benchmark_providers.py`,
      `tests/evaluation/test_cli_provider.py`,
      `tests/agent_factory/test_hermes_endpoint.py`,
      `tests/agent_factory/test_live_demo_hermes_provider.py`,
      `tests/agent_runtime/test_captain_production_adapters.py`,
      `tests/test_demo_householder_executor.py`,
      `tests/test_live_demo_operations.py`, live Nebius tests, ...).
- [ ] `.env.example`, README: Anthropic/shim as default; Nebius optional.

## Phase 4 - Captain MCP server

Thin MCP server over the gateway (bearer token per agent identity from
Phase 1). No generic `/blocks` write tool.

- [ ] Read tools: `GET /batches`, `/batches/{id}`, `/batches/{id}/blocks`,
      `/batches/{id}/bundle`, `/capabilities?need=`,
      `/v1/runtime/operations/{id}`, run events, release status,
      `/v1/factory/jobs/{id}`. Done (2026-11-09): `list_batches`,
      `get_batch`, `get_agent`, `get_agent_outcomes`
      (`agenten/captain_mcp/server.py`, agent token).
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
      `CaptainRunStore` checkpoint (`compile_and_release`) or skip batches
      whose published content is identical.
- [ ] Planner-published batches need `GATEWAY_APPROVAL_ENABLED=true`;
      otherwise `/approve` is disabled and they stay held. Surface that as
      a startup check or a gateway capability flag.
- [ ] Write tools: approve, recovery, review, runtime commands/grants/
      revocations, factory jobs, release decision, cancel (Phase 1).
      Done (2026-11-09), opt-in via `CAPTAIN_MCP_CAPTAIN_TOKEN`, all
      annotated: `register_agent`, `retire_agent`, `grant_agent_capability`,
      `revoke_agent_capability`, `assign_batch`, `cancel_batch`.
      `grant_agent_capability` is annotated destructive (it widens
      permissions). Approval is deliberately not a tool: it is the human
      review gate and stays with the operator (security review
      2026-11-09). Open: recovery, review, runtime commands/grants,
      factory jobs, release decision.
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
- [ ] `memory-core` creates a "dreaming" cron job on boot; decide whether
      the Captain planner container disables it (unplanned LLM turns).
- [ ] The container also reaches the Captain gateway on the host (8090);
      it holds no gateway token, but the production shape should network-
      isolate the planner to the MCP server only.
- [ ] Gateway container uses about 1.3 GiB RAM idle.
- [ ] Captain role prompt/skill: plan -> constitution check -> `compile_plan`
      -> `publish_plan` -> (operator approves) -> monitor -> release
      decision; never writes raw blocks.
- [ ] Offline deterministic path stays (replay clients) so the test gate
      needs no subscription.

## Phase 6 - retire LLM planning stages

- [ ] Keep decompose/align/enrich as optional fallback; OpenClaw becomes the
      default planner once Phase 5 has an end-to-end run recorded in the
      ledger.

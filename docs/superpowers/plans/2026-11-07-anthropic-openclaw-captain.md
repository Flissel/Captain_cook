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
- [ ] Tamper evidence: `project_batch` starts from unhashed `parent["status"]`
      (`gateway/contracts.py:1090`). Hash `status` (and schema metadata) or
      derive the start status from hashed data; run `verify_chain` at gateway
      startup and periodically (today it has no production caller); give the
      gateway DB user INSERT/SELECT only on `blocks`.
- [ ] Agent identity: claims carry no `worker_id` and all workers share one
      token (`gateway/auth.py:60-80`, claim route `gateway/app.py:1567`).
      Per-agent tokens, `agent_id` in `ClaimEvent`, checked against
      `trace.worker_id`.
- [ ] `POST /v1/runtime/results` (`gateway/app.py:1479`) accepts any role;
      bind results to the agent that holds the grant.
- [ ] Captain-initiated batch cancel/revoke block, signalled to the claim
      holder.
- [ ] Unify the two `batch_done` schemas (`gateway/contracts.py:540` vs
      `:936`); scope release-policy checks to run + claim iteration
      (`gateway/release_policy.py:49-53`).
- [ ] `JSONFileStorage.load` returns `[]` on corrupt JSON
      (`blockchain/storage.py:41`) - fail closed instead.

## Phase 2 - agent lifecycle contracts

- [ ] `agent_registered` (agent_id, version, owner, spec hash)
- [ ] `agent_capability_granted` (explicit tool list, scope, expiry; current
      `CapabilityGrant` is profile-fixed)
- [ ] `agent_assigned` (batch, agent, fencing token)
- [ ] `agent_outcome_recorded`, `agent_retired`
- [ ] gateway `dead_letter` block for permanently unroutable work
- [ ] agent liveness record independent of a batch claim

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
      `/v1/factory/jobs/{id}`.
- [ ] Planning tool: `compile_plan` wrapping
      `CaptainPipeline.compile_from_plan()` (validation + canonical compile
      + publish stay deterministic).
- [ ] Write tools: approve, recovery, review, runtime commands/grants/
      revocations, factory jobs, release decision, cancel (Phase 1).
- [ ] Constitution check tool: HTTP/MCP wrapper around the rule layer of
      `ConstitutionGatekeeper` (today event-bus internal only).

## Phase 5 - OpenClaw container

- [ ] Compose service `captain-openclaw` (Node + OpenClaw + Claude Code CLI),
      persisted home volume; user logs Claude Code in once inside the
      container. Validate compose config before starting.
- [ ] `openclaw.json`: `mcp.servers.captain` -> Phase 4 server; model via
      bundled `claude-cli` backend.
- [ ] Captain role prompt/skill: plan -> constitution check -> `compile_plan`
      -> approve -> monitor -> release decision; never writes raw blocks.
- [ ] Offline deterministic path stays (replay clients) so the test gate
      needs no subscription.

## Phase 6 - retire LLM planning stages

- [ ] Keep decompose/align/enrich as optional fallback; OpenClaw becomes the
      default planner once Phase 5 has an end-to-end run recorded in the
      ledger.

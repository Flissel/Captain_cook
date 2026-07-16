# Agent-Factory architecture audit snapshot

Audit baseline: integration branch `feat/system-remediation-orchestration` at
`f0e0d84`. This is an evidence log for AF00, not a completion claim. Every item
below was verified read-only; no live workflow, model, generated container, or
external integration was executed.

## Root Captain Cook runtime

- `agenten/planning/factory.py` wires LLM decomposition/alignment/enrichment to
  deterministic work-batch and holdout release. It does not build an AutoGen
  team or n8n workflow.
- `agenten/orchestration/pipeline.py` is the event-driven runtime composition
  root. Its modularization and event-bus capability work remain in the existing
  P15-P18 remediation lane.
- `agenten/events/schemas.py` carries `correlation_id` and `root_problem_id`,
  but there is no end-to-end `trace_id`/`operation_id` contract across gateway,
  n8n, Hermes, Minibook, generated agents, and evaluation evidence.
- `agenten/delivery/service.py` is currently a placeholder. Existing Minibook
  projection covers projects, plans, and assignments, not all required source,
  build, review, test, evaluation, and release artifacts.

## AutoGen and AgentFarm/Forge paths

- `agenten/workflows` provides the supported declarative AutoGen AgentChat
  compatibility path.
- `minibook/swarm/input_parser.py` and `minibook/swarm/pipeline.py` form an
  adjacent, older Forge pipeline that can parse agent descriptions and generate
  teams. It is not controlled by the root gateway/Captain release state.
- `minibook/swarm/code_processing.py::test_generated_code` performs useful AST
  syntax and heuristic structure checks, but several missing-tool/import cases
  are WARN/SKIP and do not fail the overall result. This is insufficient for the
  requested fail-closed generated-code gate.
- `minibook/swarm/docker_ops.py::docker_run_test_with_args` can convert a
  nonzero process result into PASS when selected log substrings are present.
  Release evidence must use exit status plus typed result evidence instead.
- Forge build/run cleanup contains `docker compose down -v`. It must not be
  invoked by this program; a disposable, explicitly scoped resource contract
  is required before any generated-container integration test.

## n8n inventory

Read-only MCP search on 2026-07-16 returned ten workflows. The only
Captain-named workflow is active `captain-gate-a-mailpit` (`kFT4vFVMFJRkWXIY`).
All ten report `availableInMCP: false`; details and node contracts cannot be
read through the current MCP connection. No importable n8n workflow JSON is
tracked in this repository.

Stop gate:

- Missing: inspectable/exported workflow definition for
  `captain-gate-a-mailpit` and any intended Agent-Factory integrations.
- Blocked proof: schema compatibility, idempotency, versioning, retry behavior,
  and safe reuse cannot be verified from workflow-card metadata.
- Safe minimum: keep AF04 planning offline and create no workflow.
- User decision: enable MCP access for the relevant workflow or provide a
  sanitized export for contract review.

## Hermes boundary

- The integration commit pins the `hermes-agent` gitlink to `77d5b2d`; the
  integration worktree has that submodule uninitialized.
- The preserved primary worktree contains a clean Hermes checkout at
  `3f2a389`, newer than the pinned gitlink. It includes Codex transports and MCP
  support, but this local version difference is not authority to update the
  parent repository.

Stop gate:

- Missing: approved Hermes version and a reviewed Captain-facing contract for
  supervised Codex execution through n8n MCP.
- Blocked proof: reproducible wrapper behavior cannot be claimed against an
  uninitialized pinned revision while another worktree uses a different clean
  revision.
- Safe minimum: specify the port and test it with a fake process boundary; do
  not invoke live Codex or alter the submodule.
- User decision later: retain `77d5b2d` or authorize a separately reviewed
  Hermes gitlink upgrade after compatibility evidence exists.

## Immediate audit TODOs

- [ ] Review AF00/AF01 independently before dispatching AF02.
- [ ] Inventory exact Minibook API models/endpoints needed for every artifact
  kind and reconcile them with P02 fail-closed authentication.
- [ ] Define typed trace, operation-id, artifact, wrapper, workflow, projection,
  and release-decision schemas.
- [ ] Replace heuristic PASS and destructive cleanup assumptions in the new
  architecture; do not patch or execute the adjacent Forge path opportunistically.
- [ ] Obtain inspectable n8n workflow evidence before deciding reuse vs creation.
- [ ] Resolve the approved Hermes revision before live wrapper acceptance.

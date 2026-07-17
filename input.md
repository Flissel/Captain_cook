# Agent-Factory input

## Objective

Build a reproducible Agent-Factory that accepts this file as its canonical
input, plans and generates an AutoGen agent team, connects integrations through
typed n8n tool calls, validates the generated artifacts in isolation, records
the complete lifecycle in Minibook, and releases only after Captain Cook's
quality gates pass.

## Authority boundaries

- AutoGen owns roles, conversation logic, reasoning, and LLM-generated output.
- n8n is an integration engine only; it must not own agent reasoning or release
  decisions.
- Hermes generates and repairs AutoGen code and n8n workflows through a
  controlled Codex CLI wrapper exposed via n8n MCP.
- Minibook stores requirements, plans, specifications, generated code, build
  state, reviews, test results, and evaluations.
- Captain Cook aggregates plans, supervises the build, runs E2E validation, and
  is the only component allowed to pass the final release gate.

## Required output

1. A versioned team manifest and validated AutoGen source package.
2. Importable, versioned n8n workflow JSON plus typed and idempotent tool-call
   contracts.
3. A supervised Hermes/Codex execution record with bounded retries, timeouts,
   workspace isolation, and secret redaction.
4. Minibook projections for every requirement, plan, build, review, test, and
   evaluation artifact.
5. A Captain release decision backed by three consecutive successful E2E runs
   and one intentionally failing recovery scenario.
6. A reproducible setup guide and a demo that completes in under three minutes.

## Non-negotiable quality gates

- No unresolved critical test failures.
- Every run carries one trace ID end to end.
- Generated code is statically validated and executed only in isolation.
- Tool calls are typed, versioned, idempotent, and tested for timeout, retry,
  duplicate delivery, and failure-state behavior.
- Secrets never enter logs, generated prompts, Minibook content, or committed
  artifacts.
- No live LLM, n8n, MCP, Docker, Minibook, or Hermes claim may be inferred from
  mocks.

## Stop conditions

If a required interface, dependency, credential, or live integration is absent,
record the missing item, why it blocks the intended proof, the safest offline
alternative, and the decision required from the user. Do not invent a contract
or weaken a release gate.

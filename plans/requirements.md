# Agent-Factory requirements

## Functional requirements

- **AF-FR-01 Input:** read a versioned `input.md` and reject missing, empty, or
  structurally invalid input before side effects.
- **AF-FR-02 Planning:** Captain produces a deterministic, dependency-ordered
  plan with capabilities, acceptance criteria, golden cases, and isolated
  holdouts.
- **AF-FR-03 Team manifest:** produce a typed, versioned AutoGen team manifest
  defining roles, prompts, conversation topology, model policy, and permitted
  tools.
- **AF-FR-04 Generation:** Hermes generates or repairs AutoGen code from the
  approved manifest; generated code cannot alter its own permissions.
- **AF-FR-05 n8n contracts:** every integration is a typed, versioned,
  idempotent tool call. Existing workflows are reused only after contract
  validation; otherwise the gap and new workflow are recorded.
- **AF-FR-06 Codex wrapper:** Hermes invokes Codex through an argument-array,
  workspace-confined, timeout-aware wrapper reachable through n8n MCP.
- **AF-FR-07 Minibook:** project requirements, plans, specs, source artifacts,
  build states, reviews, tests, evaluations, and release decisions are
  idempotently projected to Minibook.
- **AF-FR-08 Supervision:** Captain enforces dependency order, leases, bounded
  retries, cancellation, replay, and at most three repair attempts per packet.
- **AF-FR-09 Evaluation:** run unit, contract, integration, E2E, and output
  evaluation stages and preserve immutable evidence.
- **AF-FR-10 Release:** only Captain can release, and only after all mandatory
  gates and three consecutive E2E successes.

## Cross-cutting requirements

- **AF-NFR-01 Traceability:** one immutable trace ID crosses Captain, gateway,
  Hermes, Codex, n8n, AutoGen, Minibook, tests, and evaluation evidence.
- **AF-NFR-02 Security:** credentials are referenced, never copied into agent
  context, logs, Minibook bodies, generated source, or committed artifacts.
- **AF-NFR-03 Isolation:** generated code is parsed, dependency-checked,
  policy-scanned, and executed in a disposable restricted environment.
- **AF-NFR-04 Reliability:** timeout, retry, duplicate delivery, stale lease,
  crash/resume, and terminal failure are deterministic and tested.
- **AF-NFR-05 Reproducibility:** pinned dependencies and documented commands
  reproduce the offline path from a clean clone.
- **AF-NFR-06 Truthful evidence:** mocks prove contracts only; live claims need
  explicit live gates and resource-safety evidence.
- **AF-NFR-07 Performance:** the documented demo completes in less than three
  minutes on the reference local environment.

## Required artifacts

- Updated `input.md` and this `plans/` specification set.
- AutoGen team manifest and generated source package.
- Importable n8n workflow JSON and schemas.
- Controlled Codex CLI wrapper with n8n MCP adapter.
- Minibook persistence/projection integration.
- Unit, contract, integration, E2E, security, and recovery tests.
- Evaluation report, demo script, and reproducible setup instructions.

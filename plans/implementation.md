# Agent-Factory implementation work packages

All packets use a dedicated branch/worktree, a failing acceptance test before
behavioral code, independent spec review, independent quality review, and at
most three repair attempts. Shared files and this plan remain orchestrator
owned.

| Packet | Depends on | Owns | Acceptance criteria |
|---|---|---|---|
| AF00 Repository/integration audit | existing P00 | planning docs only | Inventory root, AgentFarm/Forge, AutoGen, n8n, Hermes, Minibook, gateway, tests, and live dependencies; every unknown becomes an explicit TODO |
| AF01 Contract design | AF00 | `plans/**` | Requirements, architecture, implementation, and test specs pass architecture review with versioned schemas and stop conditions |
| AF02 Input contract | AF01, P09 | input/planning adapter and tests | `input.md` fingerprint is stable; empty/invalid input fails before release or network calls |
| AF03 Team manifest | AF02, P10 | new manifest domain module and tests | Roles, topology, model policy, and allowed tools validate; unknown tools/roles fail closed |
| AF04 Integration catalog | AF03, P11 | n8n contract catalog/adapter and contract tests | Reuses only schema-compatible workflows; mismatches produce a documented gap without execution |
| AF05 Controlled Codex wrapper | D01/P11 | delivery executor port/adapter and tests | Argument arrays only, workspace confinement, timeout/cancel, redaction, versioned request/result, no shell interpolation |
| AF06 n8n MCP workflow package | AF04, AF05 | importable workflow JSON, schemas, tests | Import validation passes; calls are typed/idempotent; duplicate, timeout, retry, and error paths are proven |
| AF07 AutoGen generator | AF03, AF05 | generation service and tests | Produces valid isolated package from manifest; holdouts and secrets absent from context |
| AF08 Minibook artifact projection | P02, AF03 | Minibook client/projection contract and tests | All required artifact kinds upsert idempotently and never become command authority |
| AF09 Build/review/test state machine | AF06-AF08, gateway packets | Captain orchestration service and tests | Bounded three-attempt repair loop, leases, resume, trace continuity, terminal failure, immutable evidence |
| AF10 E2E and fault injection | AF09, P20 | E2E harness and evidence | Three consecutive successes plus one induced integration failure and successful bounded recovery |
| AF11 Evaluation/release gate | AF10 | evaluation report and release policy | Critical failures block; all mandatory gates map to evidence; only Captain emits release |
| AF12 Reproducible demo/docs | AF11, P21 | README/setup/demo/evaluation artifacts | Clean-clone offline start is reproducible and measured demo is under three minutes |

## Dispatch rule

No AF02+ implementation packet is dispatchable until AF00 and AF01 receive
specification PASS. Live n8n, Minibook, Hermes, Codex, Docker, or LLM execution
also requires its named dependency/credential gate; the deterministic offline
path is the safe fallback, not proof of live integration.

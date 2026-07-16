# Agent-Factory program index

Status: **planning and architecture audit in progress; implementation release
not authorized**.

This index is the canonical entry point for the Agent-Factory objective. The
existing remediation program remains authoritative for shared runtime,
gateway, setup, branch, and integration work.

## Specifications

| Document | Purpose | State |
|---|---|---|
| [Requirements](requirements.md) | Functional and quality requirements | Drafted from `input.md` |
| [Architecture](architecture.md) | Component authority, ports, data flow, current gaps | Evidence-based draft |
| [Audit snapshot](audit.md) | Repository and connected-system evidence, blockers, decisions | In progress |
| [Implementation](implementation.md) | Dependency-ordered work packages and acceptance criteria | Ready for review |
| [Test specification](test-spec.md) | Unit, contract, integration, E2E, evaluation, and release gates | Ready for review |

## Existing authoritative program documents

- `docs/superpowers/plans/2026-07-16-remediation-program-orchestration.md`
  owns branch locks, dependency order, and integration gates.
- `docs/superpowers/IMPLEMENTATION_ACK.md` is the orchestrator-only live control
  board.
- `docs/ARCHITECTURE.md` and `docs/WORKSTREAMS.md` describe existing seams, but
  their known stale sections remain P21-owned and must not be treated as proof
  of the finished Agent-Factory.

## Current evidence and gaps

- The root `agenten/planning` pipeline can decompose, align, enrich, and release
  validated JSON work-batch and holdout contracts; it does not generate an
  AutoGen team.
- `minibook/swarm` contains an older, adjacent Forge pipeline that parses agent
  descriptions and generates/runs teams. It is not yet governed by the root
  Captain release state machine or the MariaDB gateway truth.
- No tracked importable n8n workflow JSON currently implements the required
  Agent-Factory contract.
- Setup and Minibook adapters exist, but the controlled Hermes-to-Codex wrapper
  exposed through n8n MCP is not yet a proven end-to-end interface.
- A common trace-ID contract, isolated generated-code validation gate, three-run
  E2E release proof, evaluation report, and sub-three-minute demo remain open.
- The connected n8n instance exposes ten workflow cards, but every workflow has
  MCP visibility disabled. The relevant active `captain-gate-a-mailpit`
  workflow cannot yet be inspected for contract reuse.

## Immediate sequence

1. Complete and review AF00/AF01 without changing Agent-Factory production
   behavior.
2. Finish the already-dispatched remediation packets and integrate only after
   independent spec and quality reviews.
3. Dispatch AF02 onward through isolated workers using the lock/dependency rules
   in the implementation spec.
4. Keep all live integrations opt-in and separately evidenced.

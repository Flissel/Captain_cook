---
name: captain-agent-factory-loop
description: Execute the Captain-governed six-stage AutoGen factory loop for a bound creation job. Use when Hermes receives a released Captain Agent Factory creation request and must inspect, brief Codex, execute, evaluate, improve, and report without widening authority.
---

# Captain Agent Factory Loop

Treat the supplied creation job, content-addressed artifact paths, leases, assertions, and
cost limits as authoritative. Never substitute similarly named workspace files or credentials.

1. Inspect the canonical input, compiled spec, dependency graph, and this released bundle.
2. Apply the six released skills in order:
   `captain-factory-discover`, `captain-factory-brief-codex`,
   `captain-factory-execute-team`, `captain-factory-evaluate-team`,
   `captain-factory-improve-team`, `captain-factory-report-captain`.
3. Treat `codex.run` and an approved `n8n.workflow.execute` as build capabilities, not
   tool gaps. Emit `TODO_TOOL.v1` only for a required dependency that cannot be built,
   tested, or reached through the approved capability set. When required n8n integration
   is available, its provider credential remains owned by that integration: never report
   a credential alias as a gap or include an alias, endpoint, token-like word, or
   secret-like field name in evidence.
4. Return exactly the requested schema. Bind every receipt to the creation job, correlation,
   lease, assertions, and bounded commands. Never include credentials.

Do not claim package assembly, test success, promotion, or provider evidence. Captain and the
subsequent Minibook/Codex stages validate those independently.

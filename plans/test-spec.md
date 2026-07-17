# Agent-Factory test specification

## Test layers

1. **Unit:** schema validation, fingerprints, policy, retry decisions, trace
   propagation, redaction, release predicates.
2. **Contract:** AutoGen manifest, n8n tool request/result, Hermes/Codex wrapper,
   gateway events, Minibook projection, artifact hashes and versions.
3. **Integration:** disposable gateway/MariaDB; import-validation of n8n JSON;
   fake process boundary for wrapper failure modes; Minibook HTTP contract.
4. **Isolated execution:** generated package runs with a restricted workspace,
   explicit dependency set, timeout, no inherited secrets, and captured hashes.
5. **E2E:** `input.md` through planning, generation, integration simulation,
   validation, evaluation, Minibook projection, and Captain release decision.
6. **Live evidence:** separately selected tests for real n8n/MCP, Hermes/Codex,
   Minibook, model, or Docker behavior; never included implicitly in unit gates.

## Mandatory scenarios

- Invalid/empty input produces no side effect.
- Unknown role/tool and incompatible existing workflow fail closed.
- Duplicate n8n operation returns the original result without replaying a side
  effect.
- Timeout and transient failure follow bounded retry; permanent failure reaches
  a durable terminal state.
- Crash after artifact creation resumes without duplicating the artifact.
- Trace ID remains identical across every stored event and projection.
- Secret canaries do not appear in logs, prompts, source, workflow JSON,
  Minibook bodies, or evaluation output.
- Holdouts are unavailable during generation and only revealed to the evaluator.
- Tampered generated code or artifact hash blocks execution/release.
- One intentionally failing integration run is diagnosed and repaired within
  three attempts.
- Three new consecutive E2E executions pass before release.

## Release evidence table

Every gate records command, environment, commit, trace ID, timestamps, result,
skips, warnings, artifact hashes, and cleanup proof. A green unit suite cannot
substitute for an integration or live requirement. The release gate fails when
evidence is absent, stale, indirect, skipped, or produced from a different
candidate commit.

## Final commands

The final packet must run the repository gates from `AGENTS.md`, the isolated
MariaDB gateway script, full Pester setup tests, workflow import validation,
the isolated generated-code harness, the induced-failure E2E, and three
consecutive successful E2E runs. Exact commands will be frozen in AF10 once the
ports and workflow IDs exist; until then no live-complete claim is allowed.

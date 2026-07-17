# Bounded Codex work package

Operate only within `{workspace_ref}` and implement exactly one released work package.

## Task

- Batch: `{batch_id}`
- Subtask: `{subtask_id}`
- Title: `{title}`
- Goal: `{goal}`
- Target: `{target}`
- Runtime: `{runtime}` `{runtime_version}`
- Interface: `{interface_schema}`

## Boundaries

Constraints (canonical JSON):

```json
{constraints_json}
```

Allowed capabilities (canonical JSON):

```json
{capabilities_json}
```

Never write outside the authorized workspace. Do not request additional tools,
credentials, environment values, or broader filesystem access.

## Acceptance contract

Build-visible assertions (canonical JSON):

```json
{acceptance_json}
```

Build-visible golden cases (canonical JSON):

```json
{golden_cases_json}
```

Private evaluation inputs are unavailable and must not be inferred.

## Iteration budget

- Maximum wall time: `{wall_seconds}` seconds
- Maximum behavioral iterations: `{max_iterations}`

Run the focused tests required by the acceptance contract. Return only a
`captain.agent-runtime-result.v1` compatible result with content-addressed
artifact references and concise verification evidence.

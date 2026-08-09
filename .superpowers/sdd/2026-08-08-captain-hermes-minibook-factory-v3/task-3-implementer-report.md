# Task 3 implementer report

Status: DONE

## Scope

- Preserved the existing V1/V2 creation routes and added schema-dispatched V3 submit, status, result, and cancel routes.
- Added constant-time bearer authentication for a configured worker identity.
- Added assignment polling, identity-bound claim replay/conflict handling, effect submission, and scheduler continuation after accepted effects.
- Kept assignment/effect identity, replay, deadline, lifecycle, and effect-key authority in the durable Task 2 store.
- Added path-free CAS metadata lookup; responses contain only SHA-256 and byte length.
- Added typed V3 client methods that narrow unknown HTTP JSON before contract validation.
- Added an explicit `V3CreationPipeline` dependency boundary and fail-closed production composition requiring store, CAS, validator, worker token, and worker identity.
- Kept disabled creation free of Forge, Docker, and LLM imports.

## TDD evidence

Initial RED command:

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_api.py minibook/tests/test_swarm_api_client.py minibook/tests/test_creation_cli.py -q --no-cov
```

Initial RED result: collection stopped with `ModuleNotFoundError: No module named 'minibook.swarm.v3_pipeline'`. This was the expected missing Task 3 production boundary; no production implementation existed before the test.

The first implementation pass then exposed one test-fixture import error (`CreationJobV3` missing from the test import list): `1 failed, 29 passed`. After correcting that test import, the focused suite was GREEN.

## GREEN verification

- Required focused command: `30 passed, 1 warning in 2.22s`.
- Broader creation regression set (contracts, V3 state/resume/scheduler, pipeline adapter, CAS/export, API/client/CLI): `140 passed, 1 warning in 8.41s`.
- Entire `minibook/tests`: `199 passed, 1 deselected, 1 warning in 9.25s`.
- `python -m compileall -q minibook/swarm minibook/src`: exit 0.
- `git diff --check`: exit 0; Git emitted only CRLF normalization notices.

The pytest warning is the existing Starlette `httpx` deprecation. The environment also emitted an existing Requests dependency-version warning after pytest completion. No live provider, Docker, deployment, external service, OpenFang registration, promotion, or `ready_to_use` behavior was exercised or claimed.

## Self-review

- Bearer values are compared through `hmac.compare_digest`; no worker token is returned or persisted by this task.
- The claim body must match the identity bound to the bearer configuration. A second configured worker receives 409 on the claimed assignment and 403 when attempting to submit the first worker's effect.
- The path assignment ID and effect assignment ID must match before the store validates all remaining effect-key and job bindings.
- Scheduler continuation happens only after the durable store accepts the effect and advances the job to `validating`.
- V3 endpoints reject legacy jobs; the compatibility `/api/v1/creation-jobs` boundary dispatches V1, V2, and V3 by exact schema.
- Artifact metadata validates the requested digest against current bytes and never serializes the CAS root or local object path.
- V3 configuration is opt-in. Partial configuration raises before the service starts, and the validator must expose both validation and result-assembly callables.
- `V3CreationPipeline` is intentionally a Task 3 composition seam. Strict team/package validation and deterministic packaging remain Task 4 work and are not claimed here.

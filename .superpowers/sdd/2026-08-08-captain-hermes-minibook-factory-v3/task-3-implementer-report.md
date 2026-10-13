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

## Spec-review durable handoff and assignment-query correction

The spec review found that the first implementation composed a V3 validator but did not publish the durable build assignment before validation, and the assignment polling route queried raw assignment rows without proving current lifecycle/version/deadline claimability.

Behavioral RED command:

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_api.py::test_next_assignment_exposes_only_current_claimable_or_same_worker_replay minibook/tests/test_creation_cli.py::test_production_v3_pipeline_yields_assignment_then_consumes_exact_durable_effect -q --no-cov
```

RED result: `2 failed`. The store lacked `next_claimable_build_assignment`, and the production V3 runner returned a terminal validation result on its first slice rather than persisting an assignment and yielding `awaiting_build`.

The correction adds a typed store query that joins each assignment to its typed V3 job, current head, claim, and accepted effect. It returns only a non-expired current `awaiting_build` assignment or the same worker's current `building` replay; cancelled, blocked, failed, succeeded, stale-version, awaiting-user-input, expired, other-worker, and effect-accepted records are not exposed. The claim transaction remains final authority.

The production V3 pipeline now completes deterministic planning, builds and publishes one typed assignment through the store, and returns only after the store has durably entered `awaiting_build`. It does not call either build-effect validation or package validation on that initial slice. After claim/effect acceptance and rescheduling, it loads the exact typed durable `FactoryBuildEffectV2`, rejects a missing or differing runner receipt, passes that exact effect to the injected validator, and records its canonical body as the external effect receipt. A crash between external-receipt recording and step completion replays only after comparing the runner receipt with the same durable effect.

Fresh correction verification:

- Behavioral correction tests: `2 passed, 1 warning in 1.28s`.
- Focused Task 3 suite: `32 passed, 1 warning in 2.73s`.
- Task 2 state/runner regression: `60 passed in 5.23s`.
- Entire `minibook/tests`: `201 passed, 1 deselected, 1 warning in 8.47s`.
- `python -m compileall -q minibook/swarm minibook/src`: exit 0.
- `git diff --check`: exit 0; Git emitted only CRLF normalization notices.

The warning/non-claims remain unchanged: the Starlette `httpx` warning and environment Requests dependency warning are pre-existing; no live provider, Docker, external service, Task 4 packaging, promotion, deployment, or OpenFang behavior was exercised or claimed.

## Quality-review lifecycle-auth, replay-scheduling, and acknowledgement correction

The quality review identified three HTTP-boundary gaps: lifecycle routes lacked a credential separate from the worker token, a callback failure after durable submission stranded identical replays, and the effect route read progress after scheduling so a fast continuation changed the acknowledgement body.

RED evidence was captured in two bounded runs. The three-test run initially reported `3 failed` because the router had no `creation_api_key` composition parameter. With the two scheduling tests isolated from that missing parameter, both failed for their intended behaviors: the identical replay made no second schedule attempt, and the effect response returned `packaging` after the immediate callback instead of the durable post-accept `validating` acknowledgement.

The correction adds `MINIBOOK_CREATION_API_KEY` as a separate Captain/caller credential. Every legacy and V3 submit/status/result/cancel handler authenticates it with `hmac.compare_digest` before parsing input or reading job existence. Missing and incorrect callers return 401 and 403 respectively; a valid worker token remains unauthorized for lifecycle routes. Worker assignment, claim, effect, and artifact operations continue to use only the worker credential. Explicit V3 startup now fails closed if the caller credential is absent, while disabled creation still avoids Forge, Docker, and LLM imports.

Identical canonical submissions now attempt scheduling whenever the durable head is one of the scheduler's resumable states. A changed replay still conflicts before scheduling, and the scheduler remains the idempotent execution authority. Effect acceptance now captures the typed `validating` progress immediately after the durable store transition and before invoking the scheduling callback, so the response is deterministic even if that callback advances the job synchronously.

Fresh correction verification:

- Three quality-review regression tests: `3 passed, 1 warning in 0.92s`.
- Focused Task 3 suite: `36 passed, 1 warning in 2.71s`.
- Task 2 state/runner regression: `60 passed in 5.01s`.
- Entire `minibook/tests`: `205 passed, 1 deselected, 1 warning in 8.59s`.
- `python -m compileall -q minibook/swarm minibook/src`: exit 0.
- `git diff --check`: exit 0; Git emitted only CRLF normalization notices.

The warnings and non-claims remain unchanged from the prior sections.

## Quality re-review pre-validation credential boundary correction

The quality re-review demonstrated that handler-local lifecycle authentication did not run before FastAPI parsed request bodies and typed UUID path parameters. Consequently, an unauthenticated malformed request could receive a detailed 422 response before caller authentication. It also found that composition did not reject a caller credential equal to the worker credential.

RED command:

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_api.py -q --no-cov
```

RED result: `8 failed, 12 passed`. Seven representative legacy/V3 submit, status, result, and cancel requests returned 422 instead of 401 when malformed and unauthenticated, and equal caller/worker credentials did not raise. A first route-dependency pass fixed UUID-path ordering but intentionally remained RED for three malformed JSON-body cases, proving that ordinary FastAPI dependencies were not an adequate pre-body-parse boundary.

The correction places legacy and V3 lifecycle routes behind a dedicated `APIRoute` handler that authenticates the raw Authorization header before invoking FastAPI's parsing/validation handler. Missing authentication therefore returns 401 before malformed JSON or UUID details are processed; an authenticated malformed request still receives the normal typed 422. Authentication also necessarily precedes store lookup. Worker routes remain on the separate worker credential boundary. Router construction now uses `hmac.compare_digest` to reject equal configured caller and worker credentials before registering routes.

Fresh correction verification:

- Focused Task 3 suite: `44 passed, 1 warning in 3.35s`.
- Task 2 state/runner regression: `60 passed in 6.41s`.
- Entire `minibook/tests`: `213 passed, 1 deselected, 1 warning in 9.99s`.
- `python -m compileall -q minibook/swarm minibook/src`: exit 0.
- `git diff --check`: exit 0; Git emitted only CRLF normalization notices.

The warnings and non-claims remain unchanged from the prior sections. The earlier quality-review wording that handler-local auth ran before parsing is superseded by this behavioral correction and evidence.

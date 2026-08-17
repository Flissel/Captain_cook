# Task 7 Action Inbox quality-fix report

## Status

Offline quality-fix scope verified on `codex/captain-hermes-minibook-factory-v3`
from `c35460b2b35cd9248a94e9c23d247958e1524908`.  The change is ready for its
scoped commit.

## Delivered decisions

- Projection and continuation sinks use typed, digest-bound receipts and
  receipt lookup to reconcile restart/replay without duplicating an effect.
- Credential writes reserve first, look up a typed receipt before and after a
  sink error, and complete only when tenant, request, version, purpose, opaque
  reference, and reference digest all match.
- Invalid typed receipts are revoked; malformed/untyped receipts never supply
  an opaque reference and the durable reservation is released in both request
  handling and startup recovery.
- Startup reaps outstanding credential reservations only when the enabled
  Action Inbox dispatcher is present, so validation-only Gateway applications
  do not initialize MariaDB.
- Resume recovery is capped by `max_in_flight`; action listing and repository
  scans use bounded, cursor-based pages with a hard limit of 100.

## RED to GREEN evidence

- RED: `python -m pytest --no-cov tests/gateway/test_factory_action_store.py -q -k "untyped_credential_receipt or credential_reaper_releases_untyped"`
  - Result: `2 failed`; an untyped adapter result masked the receipt conflict
    with an attribute error and retained its durable reservation.
- GREEN: same command after the narrow coordinator cleanup change
  - Result: `2 passed, 80 deselected`.
- Regression diagnostic: `python -m pytest --no-cov tests/gateway -q`
  - First result: `6 failed, 472 passed, 132 skipped`; the new lifecycle hook
    eagerly initialized MariaDB for validation-only apps.
- GREEN regression slice:
  - `python -m pytest --no-cov tests/gateway/test_factory_budget.py -q -k "validation_errors_never_echo"` -> `5 passed, 26 deselected`
  - `python -m pytest --no-cov tests/gateway/test_factory_repository.py -q -k "business_benchmark_artifact_route_rejects_malformed_sha256"` -> `1 passed, 36 deselected`

## Final verification

- `python -m pytest --no-cov tests/gateway/test_factory_action_store.py tests/gateway/test_portal_api.py tests/agent_factory/test_action_resume.py -q` -> `110 passed, 7 skipped`.
- `python -m pytest --no-cov tests/gateway -q` -> `478 passed, 132 skipped`.
- `python -m pytest --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py -q` -> `18 passed`.
- `python -m compileall -q agenten blockchain chats config gateway` -> passed.
- `git diff --check` -> passed.
- Changed-file credential-shaped literal scan -> no findings.

## Non-claims

`TEST_MARIADB_DSN` was not configured, so the 132 MariaDB-gated Gateway tests
were skipped. These offline checks do not claim a live credential adapter,
MariaDB integration, Minibook sink, scheduler, provider, or deployment run.

## Authority hardening round 1

### Delivered decisions

- A `deny` response projects the typed decision with redacted `status=denied`,
  then reaches the durable `denial_acknowledged` terminal state. It never
  schedules a continuation, including on replay.
- Credential upload is a dedicated, fenced, thread-pooled secret boundary. It
  links only an opaque credential reference and does not answer or enqueue.
  Only the explicit typed response route answers and enqueues.
- `GatewayStore` no longer accepts a raw credential request or secret value.
  The app-level dedicated coordinator sends the secret only to the credential
  adapter; Gateway persistence receives reservation metadata and opaque refs.
- Every Action Inbox route checks the enabled dispatcher before store access;
  the credential route parses its raw body only after that fence. List, read,
  answer, and credential store calls use worker threads for blocking work.
- Credential lookup `None` or untyped results preserve the durable reservation
  for authoritative later reconciliation. Reaping receives a live clock
  callable, and a durable linked-reference read is checked before revocation.

### RED to GREEN evidence

- RED: `python -m pytest --no-cov tests/agent_factory/test_action_resume.py -q -k denied_action`
  - Result: `1 failed`; deny projected as answered and scheduled a continuation.
- GREEN: same command -> `1 passed, 17 deselected`.
- RED: `python -m pytest --no-cov tests/gateway/test_portal_api.py -q -k "factory_action_routes_are_authenticated or disabled_action_inbox_fences_list"`
  - Result: `2 failed`; credential upload enqueued, and disabled list/read/raw
    credential calls reached parsing or normal handling.
- GREEN: same command -> `2 passed, 17 deselected`.

### Final verification

- `python -m pytest --no-cov tests/gateway/test_factory_action_store.py tests/gateway/test_portal_api.py tests/agent_factory/test_action_resume.py -q` -> `112 passed, 7 skipped`.
- `python -m pytest --no-cov tests/gateway -q` -> `479 passed, 132 skipped`.
- `python -m pytest --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py -q` -> `18 passed`.
- `python -m compileall -q agenten blockchain chats config gateway` -> passed.
- `git diff --check` -> passed; changed-file credential-shaped literal scan -> no findings.

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

## Terminal-state hardening round 2

### Delivered decisions

- Credential expiry and failed cleanup retain the reservation and persist the
  `revoke_pending` phase, reservation key, retry count, and last-attempt time.
  The reaper retries across restart; it clears only after successful revoke or
  an authoritative absent lookup for an already revoke-pending reservation.
- MariaDB expiry transitions credential-bearing rows to `revoke_pending`
  instead of nulling the reservation. Row parsing validates the resulting
  metadata matrix.
- A dedicated Action Inbox route wrapper rejects disabled list, read, response,
  and credential routes before FastAPI validates query/path/body input. Other
  portal routes retain their existing routing behavior.
- Consumed `denial_acknowledged` rows now validate only when `consumed_at`
  exactly equals the projection acknowledgement. Reload preserves the terminal
  decision state without any continuation key.

### RED to GREEN evidence

- RED: `python -m pytest --no-cov tests/gateway/test_factory_action_store.py tests/gateway/test_portal_api.py tests/agent_factory/test_action_resume.py -q`
  - Result: `2 failed, 110 passed, 7 skipped`; the prior clearance assertion
    and SQL-expiry expectation exposed the former lossy reservation semantics.
- GREEN: same command after the durable phase, route fence, and matrix changes
  -> `116 passed, 7 skipped`.

### Final verification

- `python -m pytest --no-cov tests/gateway -q` -> `483 passed, 132 skipped`.
- `python -m pytest --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py -q` -> `18 passed`.
- `python -m compileall -q agenten blockchain chats config gateway` -> passed.
- `git diff --check` -> passed; changed-file credential-shaped literal scan -> no findings.

### Non-claims

`TEST_MARIADB_DSN` remained unset; MariaDB-gated tests were skipped. The
round-trip parser and SQL-shape tests are offline evidence only and do not
claim a live MariaDB or external credential-adapter run.

## Revoke recovery ordering round 3

### Delivered decision

- Startup reconciliation validates a typed receipt but gives an existing
  `revoke_pending` decision precedence over every complete/link path. It
  invokes cleanup by reservation key/reference; a revoke failure remains
  durable and is retried on the next restart.

### RED to GREEN evidence

- RED: `python -m pytest --no-cov tests/gateway/test_factory_action_store.py -q -k unexpired_revoke_pending_restart`
  -> `1 failed, 85 deselected`; the unexpired reaper linked the receipt without
  issuing a revoke.
- GREEN: same command -> `1 passed, 85 deselected`.
- Focused Task 7: `python -m pytest --no-cov tests/gateway/test_factory_action_store.py tests/gateway/test_portal_api.py tests/agent_factory/test_action_resume.py -q`
  -> `117 passed, 7 skipped`.
- `python -m compileall -q gateway` and `git diff --check` -> passed.

## Unbound credential revocation hardening round 4

### Delivered decision

- Restart reconciliation never forwards an opaque credential reference until
  the typed receipt has passed reservation-key, tenant, request, version,
  purpose, tenant-reference, and digest validation. A conflicting typed
  receipt is cleaned up only by the trusted reservation key with
  `credential_ref=None`; if the adapter cannot perform authoritative key-only
  revocation, the durable `revoke_pending` reservation remains for a later
  reconciliation. A validated receipt may still supply its exact reference.
- The same validated-reference fence protects request-time cleanup after a
  receipt conflict, so an unbound reference is never accepted as revocation or
  linked-credential authority.

### RED to GREEN evidence

- RED: `python -m pytest --no-cov tests/gateway/test_factory_action_store.py -q -k "unexpired_revoke_pending_restart or revoke_pending_restart_never_uses_conflicting"`
  -> `1 failed, 1 passed, 85 deselected`; the conflicting tenant-B receipt
  caused `revoke_credential` to receive
  `credential://tenant-b/another-request`.
- GREEN: same command after the validated-reference fence
  -> `2 passed, 85 deselected`.
- Normal validated-path regression records two restart revoke attempts with
  the exact validated tenant-A reference, while the adversarial spy records
  only `(reservation_key, None)` and the unavailable key-only adapter leaves
  the reservation in `revoke_pending` without linking it.

### Final verification

- `python -m pytest --no-cov tests/gateway/test_factory_action_store.py -q`
  -> `83 passed, 4 skipped`.
- `python -m pytest --no-cov tests/gateway/test_factory_action_store.py tests/gateway/test_portal_api.py tests/agent_factory/test_action_resume.py -q`
  -> `118 passed, 7 skipped, 1 warning`.
- `python -m pytest --no-cov tests/gateway -q`
  -> `485 passed, 132 skipped, 1 warning`.
- `python -m pytest --no-cov tests/test_architecture_fitness.py tests/test_import_boundaries.py tests/test_workstream_docs.py -q`
  -> `18 passed`.
- `python -m compileall -q agenten blockchain chats config gateway`,
  `git diff --check`, and the added-line credential-shaped literal scan
  -> passed with `0` credential-shaped findings.

### Non-claims

`TEST_MARIADB_DSN` remained unset, so MariaDB-gated tests were skipped. The
offline dependency set also emitted the pre-existing Requests compatibility
warning and one Starlette TestClient deprecation warning. No live credential
adapter, MariaDB, Minibook sink, scheduler, provider, or deployment run is
claimed.

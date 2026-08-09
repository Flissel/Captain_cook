# Task 2 fix implementer report

Status: DONE

## Scope

- Enforced stored-`CreationJobV3` authority for assignment publication.
- Required a live claimed assignment and exact typed effect binding before effect acceptance.
- Added immutable durable action-request/response projections with expected-version pause, resume, and expiry fencing.
- Enforced the legal V3 lifecycle and transactional expected-version fencing for step completion, cancellation, and finish.
- Removed `awaiting_build`, `awaiting_user_input`, and `building` from startup recovery; external effects/responses own continuation.
- Persisted `CreationResultV2` for V3 scheduler failures while retaining V1 results for legacy jobs.
- Kept the `d50881c` `CreationProgressV1.status` expansion in `contracts.py`; it remains required to deserialize the V3 lifecycle statuses. This fix did not otherwise change `contracts.py`.

## TDD evidence

Initial RED command:

```powershell
$env:PYTHONPATH=(Resolve-Path minibook).Path
python -m pytest minibook/tests/test_creation_v3_state.py minibook/tests/test_creation_resume.py minibook/tests/test_creation_scheduler.py minibook/tests/test_pipeline_adapter.py -q --no-cov
```

Initial RED result: `21 failed, 28 passed`. Expected failures covered missing assignment-job binding, unclaimed/expired/terminal effect rejection, missing action projection methods, illegal/unfenced transitions, paused-deadline ordering, external-wait recovery filtering, and V3 failure result typing.

Additional public action-response projection RED: the focused action pause/resume test failed with `AttributeError: CreationJobStore has no attribute action_response` after temporarily removing the implementation. Restored implementation GREEN: `1 passed`.

## GREEN verification

- Required four-file command: `49 passed in 4.15s`.
- Entire `minibook/tests`: `179 passed, 1 deselected, 1 warning in 6.19s`.
- Contract compatibility: `73 passed in 0.86s` for `minibook/tests/test_creation_contracts.py` and `tests/contracts/test_forge_contract_compatibility.py`.
- `python -m compileall -q minibook/swarm`: exit 0.
- `git diff --check`: exit 0; Git emitted only CRLF normalization notices.

Warnings were dependency/environment warnings only: a Starlette `httpx` deprecation warning and a Requests dependency-version warning. No live provider, Docker, deployment, or external service was exercised or claimed.

## Self-review

- Exact assignment replay remains immutable; foreign canonical content conflicts.
- Claim replay is limited to the same worker; another worker conflicts.
- Effect replay remains immutable but still rejects terminal or expired authority.
- Action requests and responses are stored separately so the request projection is never mutated; restart reads both typed records.
- Every head-writing operation in this scope uses one `BEGIN IMMEDIATE` transaction and an expected-version/legal-state fence where it can create a new lifecycle version.

## Spec re-review replay correction

The re-review identified that an exact accepted effect replay was limited to the immediate `validating` head. A new behavior test accepts one effect, advances legally through `packaging`, `readback`, and `succeeded`, and requires the same canonical effect to replay at every later state.

- RED: focused replay test failed at `packaging` with `CreationConflictError: assignment effect is not valid for the current building state` (`1 failed`).
- GREEN authority regression: replay plus unclaimed, expired-first-accept, terminal-first-accept, and identity-drift cases passed (`4 passed`).
- Fresh required four-file command: `50 passed in 4.79s`.
- Fresh entire `minibook/tests`: `180 passed, 1 deselected, 1 warning in 6.52s`.
- `python -m compileall -q minibook/swarm` and `git diff --check`: exit 0.

The narrow correction checks the claim and exact assignment/effect binding first, then returns an already persisted byte-identical canonical effect regardless of later legal lifecycle advancement. A changed canonical body still conflicts. Only a first acceptance reaches the deadline, terminal-state, building-state, and checkpoint-version gates, so unclaimed, expired, terminal, or otherwise invalid new effects remain rejected.

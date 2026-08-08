# Task 1 implementer report

Status: DONE_WITH_CONCERNS

## RED evidence

Both requested focused suites failed at collection before implementation because
`CreationJobV3` (and the other V3 classes) could not be imported from either
contract boundary.

## Delivered

- Added frozen, extra-forbidden V3 creation, assignment, effect, result, and
  action inbox contracts, including identity, digest, handoff, media-type,
  credential-reference, and secret-like metadata validation.
- Added six canonical JSON fixtures and parent/Minibook compatibility tests.
- Added negative coverage for source archives, digest mismatch, duplicate or
  invalid agents/handoffs, absolute paths/raw credentials, incomplete success
  results, raw credential responses, and invalid archive media types.

## Verification

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`: 11 passed.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest minibook/tests/test_creation_contracts.py -q --no-cov`: 21 passed.
- `git diff --check`: passed.

## Concern

The exact commands in the brief fail after selected tests pass because the
repository-level coverage configuration measures the entire monorepo and
requires 70%; this is unrelated to the focused contract assertions. The test
commands also emit an existing `RequestsDependencyWarning`.

## Fix round

RED commands and outcome:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`: 4 failures for missing duplicate-reference and unsafe-command validation.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest minibook/tests/test_creation_contracts.py -q --no-cov`: 3 failures for the same missing validation plus unsafe display text.

GREEN commands and outcome:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`: 17 passed.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest minibook/tests/test_creation_contracts.py -q --no-cov`: 24 passed.
- `git diff --check`: passed.

Files changed in this fix round: `agenten/agent_factory/forge_contracts.py`,
`minibook/swarm/contracts.py`, `tests/contracts/test_forge_contract_compatibility.py`,
`minibook/tests/test_creation_contracts.py`, and
`docs/superpowers/plans/2026-08-08-captain-hermes-minibook-factory-v3.md`.

Plan correction: Task 1 now names and runs the established
`tests/contracts/test_forge_contract_compatibility.py` suite; no duplicate suite
was created.

## Fix round 2

RED commands and output:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`: 2 failed, 19 passed (neutral-key Basic credential value accepted).
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest minibook/tests/test_creation_contracts.py -q --no-cov`: 2 failed, 25 passed (neutral-key bearer value and command bypass accepted).

GREEN commands and output:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`: 21 passed.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest minibook/tests/test_creation_contracts.py -q --no-cov`: 27 passed.
- `git diff --check`: passed.

This round changed both independent contract mirrors and both focused suites.
It adds recursive neutral-key secret-value rejection, display-safe scope checks,
case-normalized executable/argv restrictions, required-output and lease-binding
tests, and frozen-instance mutation evidence.

## Fix round 3

RED commands and output:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`: collection error because the new cross-contract binding helpers did not exist.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest minibook/tests/test_creation_contracts.py -q --no-cov`: collection error for the same missing helpers.

GREEN commands and output:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`: 27 passed.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest minibook/tests/test_creation_contracts.py -q --no-cov`: 30 passed.
- `git diff --check`: passed.

Both mirrors now use an exact command allowlist (`python -m pytest`,
`python -m compileall`, or direct `pytest`), reject common raw-secret value
forms under neutral metadata keys, and expose typed assignment/effect and
request/response binding validators that reject actual identity drift.

## Spec Fix Round 4

Status: DONE

RED evidence:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`:
  27 passed, 2 failed. Both mirrors rejected the new display-safe prose case
  because of the former blanket length check.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest
  minibook/tests/test_creation_contracts.py -q --no-cov`: 30 passed, 2 failed.
  It also showed that Minibook accepted pathful executable argv[0] values.
- After adding the narrow UUID safe-ID regression, the root suite was 29
  passed, 2 failed and the Minibook suite was 31 passed, 1 failed: random UUID
  metadata was incorrectly treated as an opaque token.

Delivered:

- Replaced blanket full-string length rejection with recursive token detection
  for opaque high-entropy base64/base64url/hex-like values under neutral keys.
  Display-safe prose remains accepted through the existing 500-character field
  limit. SHA-256/other digest metadata keys retain 64-lowercase-hex values;
  neutral keys do not.
- Preserved UUID and `_id` safe identifiers, typed `credential_ref` handling,
  and the established named-secret patterns.
- Replaced executable basename normalization with an exact lowercase canonical
  argv[0] allowlist: `python`, `python.exe`, `python3`, `python3.exe`,
  `pytest`, and `pytest.exe`. Path separators, drive prefixes, leading-dot and
  relative components, shell metacharacters, and launcher extensions outside
  that list fail validation.
- Kept the Captain and Minibook validators behaviorally equivalent, with
  focused coverage in both suites for prose, opaque tokens, neutral SHA-256,
  UUID IDs, and relative/pathful commands.

GREEN evidence:

- `python -m pytest tests/contracts/test_forge_contract_compatibility.py -q --no-cov`:
  31 passed in 0.79s.
- `$env:PYTHONPATH=(Resolve-Path minibook).Path; python -m pytest
  minibook/tests/test_creation_contracts.py -q --no-cov`: 32 passed in 0.49s.
- `git diff --check`: passed.

Both pytest commands emitted the pre-existing `RequestsDependencyWarning` from
the local requests/urllib3 dependency combination; it did not affect results.

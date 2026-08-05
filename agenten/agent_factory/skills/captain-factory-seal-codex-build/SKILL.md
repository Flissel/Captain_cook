---
name: captain-factory-seal-codex-build
description: Validate a Captain-issued Codex build receipt and return release-bound Hermes evidence without inspecting or reasserting build bytes.
---

# Seal Codex Build

Use only the supplied `captain.factory-skill-invocation.v1` for the
`seal_codex_build` step. Verify the job, correlation, subject version, attempt,
Tool Integrator lease, released-skill digest, input digest, workspace, assertion
IDs, and idempotency key before any effect.

1. Accept only a Captain-issued `CodexBuildReceiptV1` with `producer=captain`
   and `outcome=sealed`.
2. Verify that its assignment, creation job, build brief, workspace, Codex
   session, workspace snapshot, candidate manifest, source archive, test
   evidence, and completion timestamp satisfy the
   [evidence contract](references/evidence-contract.md).
3. Bind the receipt to the current invocation. The receipt build-brief digest
   must equal the invocation input digest; all lifecycle identities, assertion
   IDs, workspace, attempt, and idempotency key must match exactly.
4. Return `CodexBuildEvidenceV1` referencing only the Captain receipt. Do not
   inspect, execute, copy, summarize, or independently claim the source archive,
   candidate manifest, workspace snapshot, Codex session, or test results.
5. Stop fail-closed on missing, stale, duplicate, non-UTC, wrong-media-type, or
   mismatched evidence. Never create a receipt, modify code, publish a skill,
   write the ledger, claim `ready_to_use`, or bypass Captain authority.

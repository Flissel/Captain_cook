# Codex build evidence contract

The Captain-issued receipt must bind one factory job, creation job, assignment,
correlation, subject version, attempt, idempotency key, workspace, build brief,
Codex session, workspace snapshot, candidate manifest, source archive, at least
one test-evidence reference, all Captain assertion IDs, and a UTC completion
timestamp. The candidate manifest media type is `application/json`; the source
archive and workspace snapshot media type is `application/zip`.

Hermes returns only `CodexBuildEvidenceV1` with the receipt artifact reference
and the validated `CodexBuildReceiptV1`. Raw build artifacts remain Captain
receipt claims and must not be duplicated as Hermes evidence claims.

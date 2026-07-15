# Householder Runtime Plan

1. Implement a generic `HouseholderWorker` that adapts one role manifest and
   executor to the existing worker event contract.
2. Create one factory per role and route all four capability tags through the
   actual in-memory recorder lifecycle.
3. Replace the judge-facing echo-only demo with the four-role deterministic
   run and regenerate its inspectable artifact.
4. Align README, video script, workstream governance, and test expectations
   with the precise offline boundary.

Status: implemented on `feat/householder-runtime`.

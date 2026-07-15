# Householder Runtime Contract Plan

1. Map the four documented role prompts to typed runtime manifests and reject
   malformed or unregistered definitions.
2. Define the executor/report boundary so future live tools cannot leak into
   the worker or ledger contract.
3. Inject typed worker factories into the existing pipeline and protect one
   owner per capability tag.
4. Prove manifest validation, ledger-resolved factory input, and regression
   compatibility with the existing end-to-end pipeline tests.

Status: implemented on `feat/householder-runtime-contract`; the concrete role
workers remain a separate downstream branch.

# Architecture and branch-safety TODOs

> Audit date: 2026-07-15  
> Baseline: `feat/householder-runtime` at `7534eea` with local runtime work present  
> Sources: `docs/ARCHITECTURE.md`, `docs/WORKSTREAMS.md`, import-boundary tests, branch graph, and read-only merge simulations

## Current audit result

- The tested feature combinations merge without conflict markers today.
- Branch ownership is ambiguous because multiple branch names point to identical commits.
- The event-driven runtime is more developed than the architecture document describes.
- A few large orchestration and ledger modules concentrate responsibilities that should have explicit seams and contract tests.
- The documented workstream contains branch contracts that do not yet have distinct implementation commits.

## P0 — protect branch integration

- [x] Choose one canonical integration baseline and record it in `docs/WORKSTREAMS.md`. `main` is the canonical baseline for new isolated feature work.
- [ ] Resolve remaining branch aliases before new work lands:
  - Earlier runtime/contract and ledger/baseline aliases have diverged into distinct implementation branches.
  - `feat/release-evidence` and `feat/worker-fleet` currently still point to the same commit.
  - Keep aliases only when intentional; otherwise advance each branch with its owned contract or retire it after explicit approval.
- [ ] Reconcile active runtime branches with the local-delivery changes now present on local `main` before both sides edit shared root files further.
- [ ] Add a CI branch-integration job that runs `git merge-tree` (or creates a temporary merge commit) for active dependency edges from `docs/WORKSTREAMS.md`, then runs the focused contract suite.
- [ ] Assign single-branch ownership for `README.md`, `.env.example`, `requirements.txt`, and `main.py`, or move feature-specific material into owned docs/config modules. These files are the current overlap hotspot.

## P1 — align implementation with the main architecture

- [ ] Rewrite `docs/ARCHITECTURE.md` around the actual runtime path: events → decomposition → constitution gate → spawn coordinator → workers → supervisor/reaper → ledger recorder/query. Preserve the existing extension-point section as a subordinate view.
- [x] Add a machine-checkable dependency policy for package boundaries. The AST fitness gate prevents core-runtime imports from composition, demos, workflows, compatibility entrypoints, and adjacent products.
- [ ] Define the intended direction between `blockchain/` and `agenten/`. `blockchain/web_scamler.py` currently imports `agenten.functions`, reversing the otherwise ledger-as-infrastructure relationship.
- [ ] Wire `web_scamler.py` through the tool/event boundary or move it into an adapter package; do not leave it as a standalone cross-layer service.
- [ ] Decide the canonical project-definition API and place a deprecation/removal checkpoint on `chats/project_maker.py`, which duplicates the workflow entry path.
- [ ] Document top-level product boundaries for the root runtime, `minibook/`, and `hermes-agent/`: ownership, allowed imports, deployment lifecycle, and whether they are vendored products or integrated modules.
- [ ] Move root Python modules into an installable package layout and verify imports from a clean environment, as already noted in `docs/ARCHITECTURE.md`.

## P1 — close runtime contract gaps

- [ ] Replace the `AutoGenEventBus.subscribe` `NotImplementedError` boundary with an explicit supported adapter contract or a boot-time capability failure; add a test for the selected behavior.
- [x] Add a durable event/dead-letter outcome for permanently unroutable work. `SubproblemUnroutable` is emitted by the coordinator and persisted as a terminal ledger failure by the sole writer.
- [ ] Complete the distinct `feat/ledger-gateway` contract with gateway schemas and acceptance tests. Transactional MariaDB storage now exists on the branch; claim fencing and terminal-state gateway rejection remain to prove.
  - [x] Declare the MariaDB gateway as the sole production delivery truth and enforce its exclusive `MariaDBStorage` reference boundary with an AST-based architecture test.
- [ ] Keep the in-process ledger and future gateway behind one `LedgerQuery`/writer port so orchestration does not gain transport or database knowledge.
- [ ] Add an end-to-end contract test proving that every terminal worker result is recorded exactly once and can be recovered after restart or replay.

## P2 — reduce concentration and improve modularity

- [ ] Split `agenten/ledger_bridge/recorder.py` (about 1,000 lines) by responsibility: event handlers, transition/application logic, projections/indexes, and AutoGen adapter wiring. Preserve one sole-writer facade.
- [ ] Split `agenten/orchestration/pipeline.py` (about 550 lines) into composition/configuration modules while keeping `build_pipeline` as the public composition root.
- [ ] Extract shared runtime protocols from concrete in-memory implementations so offline and live adapters satisfy the same typed tests.
- [x] Add an AST-based import-graph rule covering the root Python product. Implemented in `tests/architecture_fitness.py` and enforced by `tests/test_architecture_fitness.py`.
- [x] Add architecture fitness tests for forbidden cycles and for imports crossing the root runtime, `minibook`, and Hermes boundaries.

## Verification gate for completing this plan

- [ ] Every active branch has a unique purpose, owner, and commit delta.
- [ ] All declared dependency-edge merge simulations are conflict-free.
- [ ] `python -m pytest -q` passes on each integration candidate.
- [ ] Architecture docs describe the same boundaries enforced by tests.
- [ ] README/demo claims remain limited to behavior backed by reproducible evidence.

## Audit evidence

- Focused boundary/runtime/workstream suite: 10 tests passed.
- Read-only merge simulations found no conflict markers for:
  - `feat/householder-runtime` + `feat/local-delivery-stack`
  - `feat/ledger-gateway` + `feat/householder-runtime`
  - `feat/ledger-gateway` + `feat/local-delivery-stack`
  - `feat/devpost-demo-readiness` + `feat/householder-runtime`
- Largest root-runtime modules observed: `ledger_bridge/recorder.py`, `orchestration/pipeline.py`, `supervision/supervisor.py`, `spawning/coordinator.py`, and `workers/base.py`.
- Branch evidence refreshed on 2026-07-15: `feat/ledger-gateway` contains transactional MariaDB storage; `feat/release-evidence` and `feat/worker-fleet` still share a tip.

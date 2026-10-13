# Task 4 implementer report

## Scope

- Worktree: `C:\Users\User\Desktop\Captain_cook\.worktrees\captain-hermes-minibook-factory-v3`
- Base: `f9cacf3f3abfd933f8ff1f2182e162c977bf1d6a`
- Task 4 only; no Task 5 files or behavior changed.
- Product/test files: exactly the seven paths authorized by the Task 4 plan.

## TDD evidence

- RED 1: focused collection failed with `ModuleNotFoundError: No module named 'minibook.swarm.team_package'`.
- GREEN 1: the initial focused suite reached `31 passed`.
- RED 2: the concrete V3 validator test failed because `TeamPackageV3Validator` did not exist.
- RED 3: public CAS promotion failed because validation attempted result assembly before recording successful readback.
- RED 4: referenced receipt binding failed because `validate_team_package` did not yet accept/read the effect-level receipt reference.
- RED 5: candidate binding failed with missing `artifacts` after the test required source, tool, test, and evidence digests.
- Final focused GREEN: `33 passed in 7.24s`.

## Implementation

- Added CAS-backed compiled-spec validation for exact ordered agents/handoffs, declared typed required tools, optional gaps, safe nonempty source/test paths, compileall, declared pytest, passing receipts, exact assertion IDs, forbidden tool markers, and model-invented scoring weights.
- Every effect-level Hermes, Codex, build, and test evidence reference is read with the public CAS reader and must match packaged bytes exactly.
- Added deterministic package sealing, candidate artifact digest bindings, per-file CAS publication, deterministic ZIP publication, package manifest, and canonical `minibook.cas-readback-receipt.v1` creation only after all digest reads pass.
- Added the concrete fail-closed `TeamPackageV3Validator` and wired it into the existing V3 validator port without changing Task 5 worker behavior.

## Verification

- Focused Task 4: `33 passed`.
- Task 2/3 related regression set: `103 passed`, one pre-existing Starlette/httpx deprecation warning.
- Full Minibook: `230 passed, 1 deselected`, one pre-existing Starlette/httpx deprecation warning.
- `python -m compileall -q minibook/swarm minibook/tests`: exit 0.
- `git diff --check`: exit 0 (PowerShell reports only the repository's CRLF conversion notices).
- Environment note: the isolated worktree has no local `.venv`; commands used the available Python 3.11 interpreter with `PYTHONPATH=minibook` and `--no-cov`. A pre-existing `requests` dependency-version warning is emitted at process shutdown.

## Self-review

- Confirmed source topology comes from referenced compiled JSON, never input prose.
- Confirmed receipt-only/null/incomplete snapshots remain blocked.
- Confirmed symlink/path traversal, unsafe ZIP members, archive/manifest drift, failed compile/tests/evidence, undeclared gaps, and CAS tampering fail closed.
- Corrected the validation/readback ordering defect found during self-review and added a behavioral regression test.
- No live provider, Hermes worker, Captain promotion, network execution, or ready-to-use claim is made.

## Spec correction commit

Four P1 review findings were reproduced with mutation tests before correction:

- RED: a compiled one-agent contract plus one-agent manifest could substitute for the four-agent topology frozen by `CreationJobV3`.
- RED: undeclared `.env`, secret, transcript, build metadata, and reserved publisher files were accepted as archive members.
- RED: incoming `package-manifest.json` and Minibook readback receipt names were not independently reserved.
- RED: final validation did not traverse every publisher-manifest child or require the receipt's exact complete derived ref set; a sealed source child could be changed without that final walk.

Corrections make the typed `CreationJobV3` mandatory, bind its compiled-spec ref and exact topology/tools/assertions/optional-gap policy, close input membership to declared `src/` and `tests/` files plus fixed canonical evidence, reject sensitive/reserved names, and make final validation cross-check canonical ZIP/manifest/receipt bytes, every child CAS object, all top-level refs, candidate authority, and exact receipt membership.

- Corrected focused GREEN: `44 passed`.
- Corrected Task 2/3 regression GREEN: `103 passed`.
- Corrected full Minibook GREEN: `241 passed, 1 deselected`.
- Corrected compileall and `git diff --check`: exit 0.

## Quality security correction

The quality review identified one critical host-execution flaw and four package-hardening gaps. A separate TDD cycle began at `ee52200`:

- RED: `35 failed, 1 passed` after making assignment authority and the new security mutations mandatory.
- The malicious worker-controlled test wrote a host marker under the previous subprocess pytest path.
- Archive file/size/ratio limits, casefold and Unicode-normalization collisions, Windows reserved/ADS/trailing names, malformed agent entries, and duplicate-content paths were each covered behaviorally.
- Intermediate GREEN debugging preserved named fail-closed errors and moved final assignment authority into the durable snapshot.

Execution authority is now receipt-only for Task 4:

- The validator never launches pytest, imports worker modules, creates a worker filesystem, or invokes a subprocess.
- Declared Python is bounded by archive/member/total/list limits, UTF-8 decoded, AST parsed, and compiled to bytecode without execution.
- Passing build, test, and real-case receipts are mandatory CAS references, byte-equal to packaged evidence, and exactly bound to the durable `CreationJobV3` and `FactoryBuildAssignmentV2` identities.
- No sandbox executor port is needed in this scope and no unsandboxed fallback exists. Any future execution requirement must inject a separately reviewed sandbox executor enforcing environment allowlisting, filesystem/network isolation, resource limits, bounded output, and process-tree termination.

Resource and path admission now applies assignment `limits.max_files` before decompression, plus hard compressed archive, per-member, total-uncompressed, compression-ratio, manifest-byte, and declared-list ceilings. Paths reject Unicode/casefold collisions, non-normalized Unicode, Windows reserved names, trailing dots/spaces, ADS, symlinks, traversal, and duplicate paths before any content read. Strict manifest parsing rejects malformed entries rather than filtering them. CAS readback deduplicates identical content identities while the manifest retains both distinct paths.

- Final security focused GREEN: `54 passed`.
- Final security Task 2/3 regression GREEN: `103 passed`.
- Final security full Minibook GREEN: `251 passed, 1 deselected`.
- Final security compileall and `git diff --check`: exit 0.

## Typed receipt authority correction

- RED: strict package build/real-case evidence imports failed because no exact contracts existed; generic known-ID/status JSON remained forgeable.
- Reused strict mirrored `ForgeBuildSkillUsageReceiptV1` and `CodexBuildReceiptV1` unchanged.
- Added only the absent narrow mirrored Captain `PackageBuildEvidenceV1` and `PackageRealCaseEvidenceV1` contracts.
- Authority is now `CreationJobV3 -> FactoryBuildAssignmentV2 -> FactoryBuildEffectV2 -> exact CAS receipt refs -> strict typed payloads`. Hermes binds producer/outcome, released skill, idempotency, public assertions and compiled/blueprint/tool-plan refs. Codex binds Captain sealing, assignment effect key, workspace/session/snapshot/source/manifest/test refs and assertions. Build/real-case evidence binds Captain producer/passed outcome, checkpoint/effect/source/session, plus snapshot or exact declared test paths/assertions/build evidence.
- Authentic archive-binding receipts remain external CAS inputs, avoiding a source-archive digest cycle; Minibook adds their verified bytes to the final sealed package. Worker-supplied authority-receipt member collisions fail closed.
- GREEN: focused contracts/package `95 passed`; root mirror contracts `9 passed`; related `104 passed`; full Minibook `258 passed, 1 deselected`.

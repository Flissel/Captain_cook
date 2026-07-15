# System Gap Remediation Design

> Design date: 2026-07-16  
> Baseline: local `main` at `b0038a8`  
> Scope: remediate all seven findings from the post-merge system audit

## Goal

Turn the merged Captain Cook system into an honest, repeatable Windows setup
whose lifecycle commands revalidate reality, whose default installation is
self-contained, whose integration contracts run in the required test gate,
and whose documentation describes the code that is actually shipped.

The work is managed through one master TODO plan. Each implementation session
records evidence and decisions in an append-only insight log, then consolidates
every actionable insight into a concrete task or acceptance criterion in that
same plan.

## Design principles

1. **Observed state beats checkpoints.** Checkpoints optimize resumability but
   never prove that an installed component is still healthy.
2. **The default path is self-contained.** A new user can install the complete
   supported stack without an existing VibeMind checkout. External n8n adoption
   remains an explicit opt-in mode.
3. **Status means end-to-end health.** A green system status covers every
   component promised by the setup documentation, not only a representative
   subset.
4. **Integration claims require integration evidence.** Gateway and MariaDB
   behavior cannot be declared green when its tests were skipped.
5. **One current architecture story.** README, architecture guidance,
   workstream ownership, tests, and runtime behavior must agree.
6. **Small boundaries, stable public facades.** Large modules are split by
   responsibility without forcing consumers to change imports unnecessarily.
7. **No destructive repair.** Repair and test automation must not delete Docker
   volumes, adopt foreign n8n volumes, terminate unowned processes, or expose
   secrets.

## Workstream architecture

### 1. Checkpoint revalidation and repair

Checkpoint entries become resumability hints rather than permanent success
markers. Each completed stage has a lightweight validator. On resume, a valid
stage is skipped; an invalid stage and every dependent stage are rerun. The
repair command performs the same validation, reports what it invalidated, and
then invokes the normal setup pipeline.

Stage validation follows the existing setup order:

```text
Preflight → Configuration → Captain → Hermes → Minibook → Services → Verification
```

Invalidation is downstream-only. A broken Minibook stage reruns Minibook,
Services, and Verification without reinstalling a healthy Captain or Hermes.
Checkpoint writes remain atomic and contain no secrets.

### 2. Self-contained n8n and submodule bootstrap

The default `.env.example` selects `N8N_MODE=owned` and the Compose
`owned-n8n` profile. External mode remains supported only when a reachable URL
is supplied and the user explicitly chooses adoption.

Before installing Hermes, setup initializes the repository's declared
submodules using non-destructive Git commands. It verifies that
`hermes-agent/pyproject.toml` exists afterward and returns a precise remediation
message on network, authentication, or checkout failure. Existing submodule
worktrees and local modifications are never reset or cleaned.

### 3. Executed preflight contract

The setup stage calls the existing aggregate preflight and consumes every
result. Required versions are Git 2+, Python 3.11 through 3.13, Node 20+, Docker
20+, Docker Compose v2+, Windows 11 build 22000+, PowerShell 7+, four GiB free
space, and reachable package sources.

Port checks distinguish three states:

- free: safe to start a Captain-owned service;
- occupied by a healthy endpoint configured for adoption: accepted only for
  explicitly adoptable components;
- occupied by an unknown process: fail with PID and remediation, without
  terminating the process.

After a prerequisite installation, setup re-resolves the executable and
reports `RestartRequired` if the current PowerShell process cannot see it.

### 4. Complete lifecycle health

`status.ps1 -Detailed` reports Captain demo integrity, Hermes CLI, Hermes
Minibook identity, Minibook backend, Minibook frontend, Mailpit HTTP, Mailpit
SMTP, MariaDB authentication, and n8n health. Each row includes status,
endpoint or local artifact, message, and remediation.

`start.ps1` waits for all owned processes and services, then calls the same
status contract. It returns success only when every required component is
ready. `stop.ps1` continues to stop only processes whose PID and start time
match Captain-owned metadata and only containers in Captain's Compose project.

### 5. Mandatory MariaDB and gateway gate

The repository gains one deterministic command that starts an isolated test
MariaDB service, creates a dedicated test database and credentials, exports
`TEST_MARIADB_DSN`, runs storage and gateway tests, and tears down containers
without deleting user volumes. CI invokes this gate on every integration
candidate.

The gate must prove concurrent claim fencing, terminal-state rejection,
holdout isolation, token non-disclosure, lease reclaim, asynchronous write
routes, and transactional storage. A skipped gateway or MariaDB test fails the
gate.

### 6. Documentation and ownership synchronization

`README.md` describes two distinct supported paths:

- offline demo, requiring only Python;
- complete local system, installed by `setup.ps1` and using owned n8n by
  default.

`docs/ARCHITECTURE.md` is rewritten around the actual event-driven path, with
legacy AgentChat extension points retained as a compatibility section.
`docs/WORKSTREAMS.md` names `main` as the integration baseline and records
which historical branches are merged. `AGENTS.md` and the architecture gap
backlog are updated only for behavior proven by code and tests.

### 7. Runtime and module boundaries

The AutoGen event bus exposes capabilities explicitly at boot. Unsupported
local callback subscription fails during adapter construction or capability
validation, not later during orchestration.

The URL relevance service moves out of `blockchain/` into an adapter boundary
that may depend on agent tools without reversing the ledger dependency. A
compatibility import may remain temporarily with a dated removal checkpoint.

`agenten/ledger_bridge/recorder.py` is decomposed behind its existing recorder
facade into event intake, transition application, projection/index handling,
and AutoGen wiring. `agenten/orchestration/pipeline.py` retains
`build_pipeline` as its public composition root while configuration and adapter
construction move into focused modules. Contract tests protect imports and
observable behavior throughout the split.

## Dependency and delivery order

```text
WS1 checkpoint repair ─────┐
WS2 standalone bootstrap ──┼── WS4 complete lifecycle ── WS6 documentation
WS3 executed preflight ────┘

WS5 integration gate ────────────────────────────────┘
WS7 runtime boundaries ──────────────────────────────┘
```

Workstreams 1, 2, 3, 5, and 7 can be reviewed independently. Workstream 4
consumes the validators and ownership rules from 1–3. Documentation is updated
after the behavior and gates are proven.

## Master TODO and session insight model

The implementation plan is stored at
`docs/superpowers/plans/2026-07-16-system-gap-remediation.md`. It is the only
operational backlog for this remediation effort.

Every implementation session appends an entry using this schema:

```markdown
### YYYY-MM-DD HH:MM Europe/Berlin — <session purpose>

- Evidence: `<command, test output, file:line, or runtime observation>`
- Insight: <one falsifiable statement>
- Decision: <selected behavior and reason>
- Consolidated into: `Task N, Step M` or `Acceptance criterion N`
- Supersedes: `none` or a prior insight identifier
```

Rules:

- Evidence must not contain secrets or copied `.env` values.
- An actionable insight is not complete until `Consolidated into` points to an
  exact plan item.
- Duplicate insights are merged into the existing task rather than creating a
  parallel TODO.
- Contradicted insights remain in the log and use `Supersedes` to preserve the
  audit trail.
- Checkboxes are marked complete immediately after their named verification
  passes; completion is never inferred from implementation alone.

## Error handling

Setup results continue to use the stable statuses `Ready`, `Missing`,
`Invalid`, `Failed`, `Skipped`, and `RestartRequired`. Remediation remains one
of `None`, `Install`, `Configure`, `Retry`, or `Manual`.

Failures must identify the component, preserve completed independent work,
write a non-secret checkpoint, and return a non-zero process exit. Commands do
not silently fall back from owned to external infrastructure or from real to
mock integration evidence.

## Verification strategy

Each workstream follows red-green-refactor:

1. add the smallest failing contract or acceptance test;
2. run it and record the expected failure;
3. implement only the owned behavior;
4. rerun the focused test;
5. run the affected integration gate;
6. run the full regression suite;
7. commit the independently reviewable result.

The final gate requires:

```powershell
$result = Invoke-Pester -Path scripts/setup/Setup.Tests.ps1 -PassThru
if ($result.FailedCount -gt 0) { exit 1 }
python -m pytest -q
python scripts/verify_submission.py
python -m compileall -q agenten blockchain chats config gateway
docker compose --profile owned-n8n config --quiet
```

It also requires a clean-clone Windows acceptance run and the isolated MariaDB
gateway gate. The final report must list skipped tests; no required integration
test may be skipped.

## Completion criteria

The remediation is complete only when all of the following are proven:

1. A stale completed checkpoint detects and repairs a deliberately removed
   component.
2. A recursive-clean checkout can complete setup with owned n8n and without a
   VibeMind checkout.
3. Unsupported versions and unknown port owners fail before installation.
4. Status detects failure of each promised component independently.
5. All MariaDB and gateway contracts execute with zero skips.
6. README, architecture, workstreams, agent guidance, and backlog describe the
   same current system.
7. AutoGen capability failure is boot-time explicit, the ledger dependency no
   longer points into agent tools, and recorder/pipeline facades retain their
   tested public behavior after decomposition.
8. The master plan has no unconsolidated actionable insight and every checkbox
   is backed by its named fresh verification.

## Out of scope

- Publishing or deleting Git branches.
- Migrating or deleting existing VibeMind or Captain Docker volumes.
- Adding cloud deployment, production authentication, or new third-party
  integrations.
- Replacing Hermes, Minibook, MariaDB, Mailpit, n8n, or AutoGen with different
  products.
- Broad Minibook feature work unrelated to setup and health validation.

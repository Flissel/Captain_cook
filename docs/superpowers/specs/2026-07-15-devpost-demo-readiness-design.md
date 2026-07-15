# Devpost Demo Readiness Design

## Goal

Turn Captain Cook into a credible, reproducible Developer Tools submission for
OpenAI Build Week without claiming that the planned Hermes, n8n, MariaDB, and
gateway extensions already exist.

## Product narrative

Captain Cook is an auditable agent-work orchestration prototype. A Captain
accepts a problem, decomposes it into capability-tagged subproblems, applies a
constitution gate, routes accepted work to workers, and records every state
transition in a local hash-chained ledger. Supervisory retries and recovery
make the orchestration observable rather than a black-box agent demo.

The presentable demo is deliberately a vertical slice of that product:

1. a user submits one small engineering problem;
2. a deterministic demo model decomposes it into work;
3. the gatekeeper, coordinator, echo worker, recorder, and supervisor handle
   it through the production-domain event pipeline;
4. the CLI prints a concise execution summary and writes a ledger artifact;
5. the result can be inspected and re-run offline.

The repository will distinguish this working slice from the future Captain →
Hermes → Codex delivery-fleet architecture described in the existing design
specification.

## Delivery boundaries

### Included in the first submission-ready slice

- A documented offline demo command with deterministic inputs and a generated
  ledger artifact.
- A stable public CLI entry point for that demo.
- A judge-facing README: elevator pitch, architecture, quickstart, demo,
  testing, limitations, provenance, supported platform, licenses, and Codex
  contribution evidence.
- A small evidence package that lets a judge inspect a completed run without
  configuring an API key or external service.
- A video and Devpost submission checklist with exact remaining human actions
  (recording, upload, account submission, and `/feedback` session ID).
- Regression tests for the demo artifact and CLI behavior.

### Explicitly not claimed as implemented

- A FastAPI ledger gateway or MariaDB persistence.
- Hermes workers that execute Codex CLI.
- n8n deployment, Mailpit validation, or Minibook mirroring.
- A production AutoGen Core runtime wiring. The current integrated pipeline
  intentionally uses the deterministic in-memory event bus; lower-level
  AutoGen delivery is covered separately by tests.

## Architecture

`agenten/demo.py` will be the sole presentation adapter. It constructs the
existing `SupplyChainPipeline` with an `InMemoryStorage` ledger and a
deterministic `llm_decompose` callback. It submits one fixed problem, waits
for its expected terminal subproblems, serializes a JSON artifact, and returns
a typed summary. The module must not duplicate business logic from
`agenten/orchestration/pipeline.py`.

`main.py` becomes a thin command-line dispatcher. Its default command is the
offline demo; the legacy LLM-backed exploration flow is retained only behind
an explicit `legacy` command so an API key is never needed to evaluate the
submission.

The generated artifact is a JSON document containing the problem identifier,
terminal counts, block summaries, and a verifier-friendly success flag. It
contains no credentials or model responses. A committed sample artifact is
generated from the deterministic demo and serves as a judge-inspectable
evidence snapshot.

## Error handling

- The CLI exits non-zero and writes an actionable error to stderr if the demo
  cannot reach a terminal state within its configured timeout.
- The artifact path is supplied by `--output`; parent directories are created.
- Invalid CLI arguments use standard `argparse` validation.
- The demo remains offline: no environment variables, network access, or
  external service is required.

## Verification

- Unit test: `run_demo()` returns a successful terminal summary with expected
  subproblem results.
- CLI test: `python main.py demo --output <path>` produces valid artifact JSON
  and exits successfully.
- Full regression suite: `python -m pytest -q`.
- Manual acceptance: clone, create a virtual environment, install root
  requirements, run the demo command, inspect the artifact, and run tests.

## Devpost mapping

| Devpost expectation | Repository evidence |
| --- | --- |
| Working project | Offline demo CLI and passing tests |
| Clear setup and test guidance | Root README |
| Test without rebuilding | Committed demo artifact plus repeatable command |
| How Codex and GPT-5.6 were used | README provenance section and session log |
| Public demo video under three minutes | `docs/VIDEO_SCRIPT.md` checklist and recording script |
| Code repository and licensing | Root license, third-party notices, submodule instructions |

## Completion criteria

The first slice is ready for external review when a fresh local checkout can
run the offline demo without secrets, inspect the generated or committed
artifact, run the test suite, and understand precisely which parts are working
today versus planned next.

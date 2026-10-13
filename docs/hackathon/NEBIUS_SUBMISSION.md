# Nebius x NVIDIA Global AI Hackathon — submission draft

Draft text for the Devpost "Enter a Submission" form. Deadline: **Friday,
October 30, 2026, 10:00 am Pacific Time** (19:00 Berlin).

Everything marked `PENDING` is not yet true and must be either completed or
removed before submitting. The rules require that the project "must function
as depicted in the video and/or expressed in the text description", so no
claim below may run ahead of the evidence.

## Form fields

### Project name

Captain Cook

### Tagline (one line)

An auditable captain for coding agents: NVIDIA Nemotron on Nebius Token
Factory plans, gates and judges the work, and every step lands in a
hash-chained ledger.

### Track

**Coding and Agentic Engineering** — "agents that write, run, and test code in
Token Factory". Captain Cook drives coding agents through goal rounds until
deterministic acceptance checks pass, and records the evidence.

Fallback if the sandbox path is not live by the deadline: **Best Apps and
Agents**.

### Built with

Python 3.11, NVIDIA Nemotron 3 Super / Nano, Nebius Token Factory
(OpenAI-compatible API), Hermes Agent, AutoGen 0.7 (GraphFlow), Claude Code
CLI, FastAPI, MariaDB, n8n (MCP), Docker Compose, pytest.

## About the project

### Inspiration

Building an agent is easy; proving what it did is not. Most agent frameworks
hand you a chat transcript and a "done". When a coding agent reports success,
you still cannot answer basic questions: who decided this work was in scope,
which checks actually ran, and could the agent have marked its own homework?
We wanted an orchestrator that a skeptical engineering lead could audit.

### What it does

Captain Cook is an auditable orchestrator for agent work:

1. **Plan.** The Captain turns a project brief into versioned work contracts:
   it decomposes, aligns and enriches subtasks with an LLM, then re-validates
   every model answer with deterministic code (acyclic dependencies, closed
   acceptance vocabulary, holdout tests kept out of the generation context).
2. **Gate.** A constitution gate checks each proposed subtask before any
   worker sees it. An LLM judge is a second layer; if it errors or times out,
   the work is rejected (fail-closed).
3. **Build.** A fleet of four leased Hermes roles (agent architect, tool
   integrator, real-case tester, quality warden) and AutoGen `graph_flow`
   teams build candidate capabilities. Teams iterate in explicit
   APPROVE/ITERATE verdict loops; tools are resolved to n8n workflows over MCP.
4. **Prove.** Coding goals run as `AgentGoalV1` rounds: a coding agent edits a
   sandboxed workspace, then acceptance checks run and a SHA-256 manifest diff
   detects every changed file, including attempts to touch protected test
   configuration. The agent cannot pass by editing its own tests.
5. **Record.** Every lifecycle step is written through a FastAPI gateway into
   an append-only, hash-chained MariaDB ledger with leases and fencing tokens.
   Only the Captain can promote a capability to release; Hermes, AutoGen and
   n8n cannot. Minibook shows a read-only projection for humans.

### How we use NVIDIA Nemotron and Nebius Token Factory

- **Default model provider.** Every LLM call in the system resolves through
  one provider profile (`config/llm_provider.py`). The default is Nebius Token
  Factory at `https://api.tokenfactory.nebius.com/v1` with
  `nvidia/Nemotron-3-Super-120b-a12b`; `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`
  is configured for fast, cheap calls.
- **Where Nemotron runs.** Captain planning (decompose / align / enrich), the
  constitution judge, the Hermes fleet (the key reaches only the Hermes child
  process, never the parent environment), capability evaluation and the
  business benchmark.
- **Coding agents on Nemotron.** A relay in our provider proxy translates the
  Anthropic Messages protocol (used by the Claude Code CLI) into Token Factory
  chat completions, so a coding agent's reasoning runs on Nemotron while
  Captain keeps the budget, run tokens and evidence.
  `PENDING: /v1/messages route + live relay run (M1b)`.
- **Token Factory Sandboxes.** Coding goals are designed to execute in Token
  Factory sandboxes (one checkpoint image per round, fresh judge image for
  acceptance). `PENDING: sandbox adapter + live run`.
- **Measured results.** `PENDING: live Nemotron run — model id, number of calls,
  tokens and latency from artifacts/demo-run-llm.json and the spike notes.`

### How we built it

- Event contracts and ports first; adapters behind them (in-memory bus for
  the offline demo, AutoGen runtime for live paths).
- Gateway as the sole production writer; lifecycle state is derived from
  append-only blocks, never stored as mutable status.
- Test-first throughout: about 4,100 offline tests run without any key,
  network or Docker (`pytest -m "not live"`); paid or live gates are opt-in
  and never run in the default suite.
- A deterministic offline demo (`python main.py demo`) and a judge-facing
  integrity check (`python scripts/verify_submission.py`).

### Challenges we ran into

- **Keeping coding agents honest.** A coding agent can "pass" by editing
  `conftest.py` or its own tests. We moved acceptance to a content-hash
  manifest diff with protected paths, symlink policy and cache purging, and
  plan a fresh judge image so acceptance never runs in the agent's own image.
- **Provider switch without leaks.** Moving from OpenAI to Token Factory, the
  first draft overwrote the process-wide `OPENAI_API_KEY` with the Nebius key.
  We now pass provider keys only to the child process that needs them.
- **Cross-platform digests.** Authority files are pinned by SHA-256; Windows
  CRLF checkouts broke attestation until we pinned line endings per file.
- **Protocol translation.** Strict tool-call pairing between the Anthropic and
  chat-completions formats, failing closed on malformed upstream streams.

### Accomplishments that we're proud of

- An LLM can propose, but code decides: every model output is re-validated.
- A coding agent cannot mark its own homework.
- Every side effect is traceable in a hash-chained ledger, and only the
  Captain can release.
- The whole offline path is reproducible without credentials.

### What we learned

`PENDING: write after the live Nemotron spike — e.g. tool-calling and
structured-output behaviour of Nemotron 3 Super vs Nano on our prompts.`

### What's next for Captain Cook

- Run every coding goal in Token Factory sandboxes with a fresh judge image.
- A goal-driven agent builder: a single prompt becomes a Hermes `/goal`, a
  `TO_BE_BUILT.md` brief and a team, with missing tools and credentials
  reported into the ledger instead of guessed.
- Durable dead-letter handling and a hosted multi-tenant gateway.

## Significant updates during the submission period (since August 26, 2026)

Captain Cook existed before the submission period (it was built for OpenAI
Build Week in July 2026 on OpenAI models). Since August 26, 2026 we:

1. **Moved the whole system onto Nebius Token Factory and NVIDIA Nemotron**:
   new provider resolution with Nemotron 3 Super as the default, Hermes fleet
   and business benchmark on Token Factory, key isolation for child processes,
   and a Nemotron-backed householder executor for the demo.
2. **Replaced the Codex CLI path with a goal-driven coding core**:
   `AgentGoalV1` contracts, Claude Code stream parsing, sandbox port,
   acceptance with manifest diffs, a multi-round goal runner, and
   Captain-authorized coding tools for AutoGen teams.
3. **Added an Anthropic-Messages ↔ chat-completions bridge** so coding agents
   can reason on Nemotron through Token Factory.
4. **Added declarative AutoGen `graph_flow` teams** with APPROVE/ITERATE
   verdict loops and structural validation (no unconditional cycles, no
   unreachable nodes).
5. **Composed the authenticated runtime** from digest-verified adapter ports,
   integrated the production capability bundle (developed in August, merged
   October 13) and fixed cross-platform digest attestation.
6. **Documented the architecture**: an overview plus per-part lifecycle and
   dataflow documents under `docs/architecture/`.

Evidence: `git log --since=2026-08-26` on `main` (71 commits at the time of
writing).

## Testing instructions for judges

- Repository: https://github.com/Flissel/Captain_cook (public, MIT licence).
- Offline, no key required:

  ```powershell
  py -3.11 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  .\.venv\Scripts\python.exe main.py demo --output artifacts/demo-run.json
  .\.venv\Scripts\python.exe scripts/verify_submission.py
  ```

- With Nemotron (requires a Nebius Token Factory key in `.env`):
  `main.py demo --householder-executor llm` writes
  `artifacts/demo-run-llm.json` with the model id.
- Hosted demo: `PENDING: URL`.

## Feedback on Nebius Token Factory, AI Cloud and NVIDIA tools

`PENDING: fill in from the live spike.` Topics to cover with concrete
observations:

- Onboarding and key setup; OpenAI-compatibility gaps we hit (if any).
- Nemotron 3 Super vs Nano: tool calling, structured output, latency, cost.
- Sandboxes: image/checkpoint model, startup time, limits.
- Documentation gaps and what would have saved time.

## Owner checklist before submitting

- [ ] Live Nemotron run on Token Factory recorded (Stage One requires a
      runtime call to the Token Factory inference API).
- [ ] Hosted demo URL working and reachable without local setup.
- [ ] README section on Nemotron / Token Factory updated with measured results.
- [ ] Public YouTube video under three minutes
      (`docs/hackathon/VIDEO_SCRIPT_NEBIUS.md`).
- [ ] All `PENDING` markers above resolved or removed.
- [ ] Track selected; feedback section completed (separate prize).
- [ ] Optional: a runtime call to the Tavily API would qualify for the Tavily
      bonus prize.

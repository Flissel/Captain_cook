# OpenClaw planner spike

Goal (plan `docs/superpowers/plans/2026-11-07-anthropic-openclaw-captain.md`,
Phase 5): prove that OpenClaw in a container, logged in to the Claude
subscription through its bundled `claude-cli` backend, can call Captain tools
over MCP. Scope is read-only: `list_batches` and `get_batch`.

```
OpenClaw (container, :18889 loopback)
  └─ MCP streamable-http ─> Captain MCP server (host, 127.0.0.1:18121, bearer)
        └─ HTTP, agent token "openclaw" ─> Captain gateway (host, :8090)
              └─ throwaway MariaDB (Compose project captain-openclaw-ledger, :3307)
```

Isolation: own Compose projects (`captain-openclaw`, `captain-openclaw-ledger`)
and volumes (`captain-openclaw-home`, `captain-openclaw-ledger_ledger_data`).
Nothing here touches the shared ledger on 3306, VibeMind's n8n or other stacks.
No service restarts on Docker Desktop boot.

The MCP server holds a registered *worker agent* token, never the Captain
token, and the gateway requires agent identity, so the planner cannot claim
or write through this path.

## Steps

Run from the repository root with the project venv.

1. Generate secrets once (written to the gitignored
   `deploy/captain-openclaw/.env`, never printed):

   ```powershell
   .\.venv\Scripts\python.exe scripts\openclaw_spike.py init-env
   ```

2. Start the throwaway ledger and validate the OpenClaw config:

   ```powershell
   docker compose -p captain-openclaw-ledger --env-file deploy/captain-openclaw/.env -f docker-compose.yml up -d mariadb
   docker compose -f deploy/captain-openclaw/compose.captain-openclaw.yml --profile cli config --quiet
   ```

3. Start gateway and MCP server (two terminals), then seed one batch:

   ```powershell
   .\.venv\Scripts\python.exe scripts\openclaw_spike.py gateway
   .\.venv\Scripts\python.exe scripts\openclaw_spike.py mcp
   .\.venv\Scripts\python.exe scripts\openclaw_spike.py seed-batch
   ```

4. Start OpenClaw and install Claude Code into the persisted home
   (Anthropic's native installer, as the OpenClaw Docker docs prescribe):

   ```powershell
   $c = "deploy/captain-openclaw/compose.captain-openclaw.yml"
   docker compose -f $c up -d captain-openclaw-gateway
   docker compose -f $c --profile cli run --rm --entrypoint sh captain-openclaw-cli -lc 'curl -fsSL https://claude.ai/install.sh | bash'
   ```

5. **Operator only:** log in to Claude Code inside the container (interactive
   OAuth), then route Anthropic models through the CLI backend:

   ```powershell
   docker compose -f $c --profile cli run --rm --entrypoint /home/node/.local/bin/claude captain-openclaw-cli auth login
   docker compose -f $c --profile cli run --rm --entrypoint /home/node/.local/bin/claude captain-openclaw-cli auth status --text
   docker compose -f $c --profile cli run --rm captain-openclaw-cli models auth login --provider anthropic --method cli --set-default
   ```

6. Verify the MCP link and run one planner turn:

   ```powershell
   docker compose -f $c --profile cli run --rm captain-openclaw-cli mcp doctor --probe
   docker compose -f $c --profile cli run --rm captain-openclaw-cli agent --agent main --message "Use the captain tools: list pending batches, then read spike-batch-1 and report its status."
   ```

   Success: the answer names `spike-batch-1` with status `pending`.
   Verified 2026-11-09 with OpenClaw 2026.10.4 and Claude Code 2.1.319.

Steps 5 and 6 run in an interactive terminal: `models auth login` refuses
to run without a TTY, so do not pass `-T`. The CLI service shares the
gateway's PID namespace on purpose; without it OpenClaw refuses state
access (`GatewayLockNamespaceError`).

## Captain write tools (opt-in)

The MCP server is read-only unless it gets `CAPTAIN_MCP_CAPTAIN_TOKEN`
(the gateway's Captain token). Then it also offers a fixed set of Captain
tools: `register_agent`, `retire_agent`, `grant_agent_capability`,
`revoke_agent_capability`, `assign_batch`, `cancel_batch`,
`recover_expired_claim`, and with `CAPTAIN_RELEASE_POLICY_VERSION` also
`record_release_decision` (the gateway computes the outcome from the
evidence; the planner cannot choose it).
There is no generic block write. Reads keep using the agent token. Every
tool carries MCP safety annotations (read-only, destructive, idempotent),
so OpenClaw can require approval for destructive calls. Granting a
capability counts as destructive because it widens what an agent may do.

Batch approval is not an MCP tool. It is the human review gate; a planner
that could approve its own batches would make the gate meaningless, so
approval stays with the operator (`POST /batches/{id}/approve`). For the
same reason there are no review-decision tools, and runtime commands and
Factory jobs stay with Captain's own runtime.

Giving the planner this token makes it Captain. Enable it only for a run
that is meant to orchestrate, never for the read-only spike.

## Planning tools (opt-in)

With `CAPTAIN_PLAN_TARGET` and `CAPTAIN_PLAN_CAPABILITY_TAGS` (comma
separated; optionally `CAPTAIN_PLAN_ALLOWED_TARGETS`) the server offers
`compile_plan`. OpenClaw supplies the subtasks, their alignment into
batches and one enrichment per batch; Captain validates and compiles them
deterministically and returns the batches plus holdout case counts.
`check_constitution` runs the deterministic constitution rules over every
subtask; the semantic review is not performed by the planner itself.

With the Captain token it also offers `publish_plan`. It refuses plans
the constitution check rejects, then recompiles and releases every batch as `pending_review`, so nothing is claimable until the
operator approves it. That needs `GATEWAY_APPROVAL_ENABLED=true` on the
gateway; without it the batches stay held.

## Captain planner role

`workspace/AGENTS.md` (loaded into every OpenClaw session) defines the
planner role and its hard rules; `workspace/skills/captain-plan/SKILL.md`
carries the plan format and procedure: draft -> `check_constitution` ->
`compile_plan` -> operator consent -> `publish_plan` -> operator approval
-> monitor. The seed service copies both into the workspace on every start,
so edit them here, not in the container. Tests keep the role's tool lists
in step with the real MCP surface.

The seed config allows only the Captain MCP tools plus `read`
(`tools.allow`; a deny-list left terminal, computer and file tools visible),
still denies shell/process execution and scheduler/gateway administration,
turns off the memory "dreaming" job and skips OpenClaw's own bootstrap
files. Claude Code's native tools (Write, Edit, Agent, schedulers, ...) are
denied in `claude-settings.json`, which the seed installs as
`~/.claude/settings.json`. Check tool exposure in the Claude transcript,
not by asking the model. `openclaw.json` is copied only once:
an existing volume from the spike keeps its old config, so apply those keys
there by hand (`openclaw config set ...` in the CLI container) or start
with a fresh `captain-openclaw-home` volume after asking the operator.
Verified live on OpenClaw 2026.10.4 on 2026-11-09 (see the plan, Phase 5).

## Stop and clean up

```powershell
docker compose -f deploy/captain-openclaw/compose.captain-openclaw.yml down
docker compose -p captain-openclaw-ledger -f docker-compose.yml down
```

`down` keeps the volumes (Claude Code login, spike ledger). Removing them
(`down -v` or `docker volume rm`) is a deliberate operator decision.

## Known risks

- Anthropic's terms for subscription use in third-party harnesses changed in
  2026. OpenClaw's docs treat `claude -p` as sanctioned; re-check before any
  production use. The API-key path stays the fallback.
- OpenClaw rewrites `openclaw.json` on `models auth login` (backup in
  `openclaw.json.bak`). In the 2026-11-09 run the `${...}` references
  survived; re-check after upgrades. `mcp doctor` still warns about a
  literal secret in `headers.Authorization`.
- OpenClaw's `memory-core` plugin schedules a background "dreaming" job,
  which can start model turns on the subscription while the gateway runs.
- The container can reach every host loopback service through
  `host.docker.internal`, including the Captain gateway (it holds no
  gateway token).
- Image `ghcr.io/openclaw/openclaw:2026.10.4` is pinned; override with
  `CAPTAIN_OPENCLAW_IMAGE` if that tag is unavailable.

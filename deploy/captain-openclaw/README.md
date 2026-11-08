# OpenClaw planner spike

Goal (plan `docs/superpowers/plans/2026-11-07-anthropic-openclaw-captain.md`,
Phase 5): prove that OpenClaw in a container, logged in to the Claude
subscription through its bundled `claude-cli` backend, can call Captain tools
over MCP. Scope is read-only: `list_batches` and `get_batch`.

```
OpenClaw (container, :18889 loopback)
  └─ MCP streamable-http ─> Captain MCP server (host, 127.0.0.1:8121, bearer)
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

   Success: the answer names `spike-batch-1` with status `pending`, and the
   MCP server log shows the calls.

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
- OpenClaw may rewrite `openclaw.json` on `models auth login`; check that the
  `${CAPTAIN_MCP_TOKEN}` reference survives instead of a resolved value.
- Image `ghcr.io/openclaw/openclaw:2026.10.4` is pinned; override with
  `CAPTAIN_OPENCLAW_IMAGE` if that tag is unavailable.

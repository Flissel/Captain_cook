# Runtime-composition live proof — `agentfarm.deliver` via MCP (2026-08-19)

**`captain_deliver` returned a fail-closed refusal, not a success.** The
composed runtime rejected the request with HTTP 422 `invalid runtime
command` before any Hermes/Codex work could start. No delivery happened.
This document records that refusal verbatim, plus everything that was
independently verified around it. It does **not** contain the ledger proof
(Step 4) or the cleanup proof (Step 6) — those are the controller's,
appended below in a separate section.

## Scope note

This document was produced by an agent operating under a split of
responsibilities: the agent ran Step 1 (marker), Step 3 (drive
`agentfarm.deliver` through the MCP server) and Step 5 (this document). The
controller independently ran Step 2 (pre-run ledger snapshot), and owns
Step 4 (SQL proof) and Step 6 (cleanup + its own verification query) per the
spec's rule that an agent's or tool's `ok: true`/`ok: false` report is not
evidence — only a query the controller runs itself counts.

## 1. Health checks (controller's results, quoted verbatim)

These were run and reported by the controller immediately before this task
started; the agent did not re-run them and takes them as given per the task
brief.

```
GET http://127.0.0.1:8090/healthz
→ 200 {"status":"ok","database":"ready"}

GET http://127.0.0.1:8091/health  (with bearer token)
→ 200 {"status":"ok"}

GET http://127.0.0.1:8091/health  (without a token)
→ 401
```

## 2. Pre-run ledger snapshot (controller's, stated as given)

Per the task instructions: the controller had already run Step 2 before
this agent started. Reported to the agent: `total_blocks = 0` and
`preexisting_marker_rows = 0` for marker `vm-landing-proof-20260819` — the
`blocks` table was completely empty. The agent did not re-run this query
(out of scope; see Boundaries in the task instructions) and is relaying the
controller's figure, not independent evidence.

## 3. Marker used

```
vm-landing-proof-20260819
```

Used verbatim as `project_id` in the request below.

## 4. Commands run (exact, as executed)

### 4a. Extract the MCP server from vibemind-os `origin/master`

The local vibemind-os checkout was on `feat/mcp-tool-hub` and does not
contain `scripts/mcp_servers/captain_cook_mcp.py`. It was extracted via
`git show` without touching the checkout's branch or working tree:

```bash
mkdir -p /c/Users/User/AppData/Local/Temp/claude/cc-landing
cd /c/Users/User/Desktop/Vibemind_V1/vibemind-os
git show origin/master:scripts/mcp_servers/captain_cook_mcp.py \
  > /c/Users/User/AppData/Local/Temp/claude/cc-landing/captain_cook_mcp.py
```

Result: `exit=0`, 229 lines extracted, matching the file's own header
(`Captain Cook MCP Server — agentfarm space runtime via MCP`). The
vibemind-os checkout's branch was confirmed unchanged before and after
(`feat/mcp-tool-hub`, pre-existing unrelated dirty state left untouched).

### 4b. Drive the MCP server over stdio with a full `initialize` handshake

The server (`captain_cook_mcp.py`) processes each stdin line independently
via a stateless per-line loop — it does not gate `tools/call` on having
received `initialize` first — but the handshake was performed properly
regardless, per the task's instruction not to assume the single-request
form works. Three JSON-RPC messages were sent as three stdin lines, in
order: `initialize`, `notifications/initialized`, then `tools/call` for
`captain_deliver`.

PowerShell driver script (token read from `.env` into the child process
environment; never printed or logged — the script's own output, captured
below, contains no secret material):

```powershell
$env:CAPTAIN_RUNTIME_URL = 'http://127.0.0.1:8091'
$tokenLine = (Get-Content .env | Select-String '^CAPTAIN_RUNTIME_TOKEN=').ToString()
$env:CAPTAIN_RUNTIME_TOKEN = $tokenLine.Split('=', 2)[1]

$initReq = @{
    jsonrpc = '2.0'; id = 0; method = 'initialize'
    params  = @{
        protocolVersion = '2024-11-05'
        capabilities    = @{}
        clientInfo      = @{ name = 'task6-live-proof-driver'; version = '1.0.0' }
    }
} | ConvertTo-Json -Depth 8 -Compress

$initializedNotif = '{"jsonrpc":"2.0","method":"notifications/initialized"}'

$callReq = @{
    jsonrpc = '2.0'; id = 1; method = 'tools/call'
    params  = @{
        name      = 'captain_deliver'
        arguments = @{
            project_id         = 'vm-landing-proof-20260819'
            prompt_ref         = 'prompt://landing-proof/hello'
            capability_profile = 'planner'
        }
    }
} | ConvertTo-Json -Depth 8 -Compress

$lines = @($initReq, $initializedNotif, $callReq) -join "`n"
$lines | .venv\Scripts\python.exe 'C:\Users\User\AppData\Local\Temp\claude\cc-landing\captain_cook_mcp.py'
```

Run from `C:\Users\User\Desktop\Captain_cook\.worktrees\runtime-composition`.
Exit code: `0`. Stderr: empty.

## 5. Complete raw MCP response (verbatim, unedited)

```
{"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "captain-cook", "version": "1.0.0"}}}
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{\"ok\": false, \"error\": \"captain runtime HTTP 422\", \"detail\": \"{\\\"detail\\\":\\\"invalid runtime command\\\"}\"}"}]}}
```

Decoded inner `tools/call` payload (the `text` field above, unescaped, for
readability — the escaped form above is the actual verbatim bytes received
on stdout):

```json
{"ok": false, "error": "captain runtime HTTP 422", "detail": "{\"detail\":\"invalid runtime command\"}"}
```

No `refs` (`batch_id` / `subtask_id` / `workspace_ref`) were produced. The
call failed at the `hermes.plan` step, before `codex.run` was ever
attempted — `captain_deliver`'s composite logic never got past the first
leg.

## 6. What the refusal is, as far as static reading can tell (analysis, not new evidence)

No further live calls were made after the one recorded above — this
section is read-only analysis of already-checked-out source, offered so
the refusal is not left unexplained, not an attempt to route around it or
retry with a different payload.

`agenten/agent_runtime/http_server.py` registers `invalid_runtime_command`
as the handler for FastAPI's `RequestValidationError` on `POST
/v1/runtime/execute`, returning exactly `{"detail": "invalid runtime
command"}` with status 422 — which is exactly what came back. That means
the HTTP body the MCP server posted failed Pydantic validation against the
endpoint's declared request model before any handler logic (Hermes/Codex
adapters, ledger writes) ran.

`agenten/agent_runtime/contracts.py` shows the endpoint's request model is
`AgentRuntimeCommand`, an *envelope* requiring `schema`, `event_id`,
`correlation_id`, `occurred_at`, `producer`, `subject_id`,
`subject_version`, and a nested `payload: AgentRuntimeCommandPayload` (the
`operation`/`project_id`/`prompt_ref`/`capability_profile`/`limits` fields
the MCP server does send). `captain_cook_mcp.py`'s `_execute()` posts only
the flat inner-payload fields as the top-level JSON body — it does not
wrap them in the envelope, and does not supply `event_id`, `correlation_id`,
`occurred_at`, `producer`, `subject_id`, or `subject_version` at all.
`AgentRuntimeCommandPayload` also uses `extra="forbid"`, so even the fields
it does send, sent at the wrong nesting level, would fail validation.

Read together, this looks like a contract mismatch between the
`origin/master` copy of `captain_cook_mcp.py` and the runtime entrypoint
this plan composed — the MCP server was written against a flatter request
shape than the runtime that Tasks 1-5 landed actually accepts. This is
offered as explanation, not as a verified root cause; nothing was changed
or retried to confirm it.

## 7. What this proves and what it does not

**Proven independently by this task:**
- The composed runtime is live and reachable at `POST
  /v1/runtime/execute` on `127.0.0.1:8091` and applies real Pydantic
  request validation to inbound commands (fail-closed on malformed input,
  producing a structured 422 rather than a crash or a silent accept).
- The MCP server (`origin/master`'s `captain_cook_mcp.py`) can be driven
  end-to-end over stdio (`initialize` → `notifications/initialized` →
  `tools/call`) against the live runtime, and correctly surfaces the
  runtime's HTTP-level rejection back through the MCP `tools/call` result
  rather than swallowing or misreporting it.

**Not proven by this task:**
- That `agentfarm.deliver` can successfully write a ledger block through
  the composed runtime. It did not run far enough to reach `hermes.plan`
  execution, `codex.run`, or any ledger write — the request never passed
  request validation.
- No row was created by this task for the controller to find in Step 4.
  Given the marker was previously confirmed absent (`total_blocks = 0`,
  `preexisting_marker_rows = 0`), the controller's ledger query is
  expected to still show `preexisting_marker_rows = 0` for
  `vm-landing-proof-20260819` — the honest, negative result of a request
  that never reached the persistence layer, not a gap in the query.

## Ledger proof (controller)

*(Left empty — the controller fills this in with the raw SQL output from
its own Step 4 query, per the spec's rule that only a query the controller
runs itself, against its own command output, counts as evidence.)*

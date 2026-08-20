# Runtime-composition live proof — `agentfarm.deliver` via MCP (2026-08-19)

**Both rounds ended in a fail-closed refusal, not a success — no ledger
write is expected from either.** Round 1 (via the `origin/master` MCP
server) got HTTP 422 `invalid runtime command` — a request-shape
rejection. Round 2 (a hand-built, contract-correct command posted directly
to the runtime, bypassing the MCP server's shape defect) got past the 422
but was then refused with HTTP 503 `runtime execution failed`, which
source reading (§9) traces to a **structural precondition**: the runtime's
state port requires a real, already-released work batch for every
operation — including `hermes.plan` — and this environment's `blocks`
table is confirmed completely empty, so no released batch can exist. This
document records both refusals verbatim, plus everything that was
independently verified around them. It does **not** contain the ledger
proof (Step 4) or the cleanup proof (Step 6) — those are the controller's,
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

## 8. Round 2 — a well-formed command posted directly to the runtime

The coordinator confirmed round 1's diagnosis independently (their own
Step 4 query after round 1: `total_blocks = 0`, marker rows `0` —
identical to the pre-run snapshot, no partial state left by the 422) and
directed a second round: skip the defective MCP server's request-building
code and POST a contract-correct `AgentRuntimeCommand` envelope straight to
`http://127.0.0.1:8091/v1/runtime/execute`, built from the fields read out
of `agenten/agent_runtime/contracts.py`. This is the coordinator's own
framing: sending a correctly-shaped command tests the same gate from the
legitimate side, it does not weaken or bypass it. No runtime source file,
validation, or auth handling was changed. Marker `vm-landing-proof-20260819`
was reused verbatim, again as `project_id`.

### 8a. Real artifact placed in the content-addressed store

`hermes.plan`'s `prompt_ref` must be an `ArtifactRef` object resolvable by
`ContentAddressedArtifactAdapter.require()`
(`agenten/agent_runtime/captain_production_adapters.py`), which reads
`<artifact_root>/<sha256[:2]>/<sha256>` and requires
`uri == f"artifact://sha256/{sha256}"` exactly. `artifact_root` resolves
(via `_canonical_repository_root()` in `production_bootstrap.py`, `=
Path(__file__).resolve().parents[2]`) to this worktree's own
`.captain-cook/artifacts/sha256/` — confirmed present on disk before any
write, consistent with the live server having been started from this
worktree. `.captain-cook/` is gitignored, so nothing tracked was touched.

A real file was written (not a fabricated digest) and its digest derived
from the actual bytes:

```python
# C:\Users\User\AppData\Local\Temp\claude\cc-landing\prepare_artifact.py
ARTIFACT_ROOT = r"C:\Users\User\Desktop\Captain_cook\.worktrees\runtime-composition\.captain-cook\artifacts\sha256"
CONTENT = b"vm-landing-proof-20260819 hermes.plan direct-command artifact\n"
MEDIA_TYPE = "text/plain"
digest = hashlib.sha256(CONTENT).hexdigest()
# written to <ARTIFACT_ROOT>/<digest[:2]>/<digest>
```

Run: `.venv/Scripts/python.exe prepare_artifact.py`. Output:

```json
{
  "artifact_ref": {
    "uri": "artifact://sha256/e774a26dd7216cc7447c379907d08678b0beece18e285f2add71101dcefe2eeb",
    "sha256": "e774a26dd7216cc7447c379907d08678b0beece18e285f2add71101dcefe2eeb",
    "media_type": "text/plain"
  },
  "path": "C:\\Users\\User\\Desktop\\Captain_cook\\.worktrees\\runtime-composition\\.captain-cook\\artifacts\\sha256\\e7\\e774a26dd7216cc7447c379907d08678b0beece18e285f2add71101dcefe2eeb",
  "content_len": 62
}
```

File existence and exact content were confirmed on disk (`cat` of the
written path reproduced the same bytes) before it was referenced in any
request.

### 8b. Attempt 1 — well-formed `hermes.plan` command, no `batch_id`

Envelope built to match `AgentRuntimeCommand` /
`AgentRuntimeCommandPayload` exactly: `schema` =
`"captain.agent-runtime-command.v1"`, fresh `event_id` / `correlation_id`
(UUIDv4), `occurred_at` as a UTC-aware ISO-8601 timestamp, `producer` =
`"captain"`, `subject_id` = `"vm-landing-proof-20260819"` (matches the
identifier pattern; no `subtask_id` was set, so the
`subject_matches_subtask` validator does not constrain it), `subject_version
= 1`, and a `payload` with `operation = "hermes.plan"`, `project_id =
"vm-landing-proof-20260819"`, the artifact ref from §8a as `prompt_ref`,
`capability_profile = "planner"` (satisfies `hermes.plan`'s
planner-profile requirement), and `limits = {"wall_seconds": 60,
"max_iterations": 1}`.

Driver (`C:\Users\User\AppData\Local\Temp\claude\cc-landing\direct_command_driver.py`),
run via:

```powershell
$env:CAPTAIN_RUNTIME_URL = 'http://127.0.0.1:8091'
$tokenLine = (Get-Content .env | Select-String '^CAPTAIN_RUNTIME_TOKEN=').ToString()
$env:CAPTAIN_RUNTIME_TOKEN = $tokenLine.Split('=', 2)[1]
.venv\Scripts\python.exe 'C:\Users\User\AppData\Local\Temp\claude\cc-landing\direct_command_driver.py'
```

Request body sent (verbatim):

```json
{
  "schema": "captain.agent-runtime-command.v1",
  "event_id": "6ecc8739-9e3f-49d8-b025-81a1c24c72f0",
  "correlation_id": "276bbe4a-5151-414f-a7de-e80b8103ed8c",
  "occurred_at": "2026-08-20T10:27:24.385793+00:00",
  "producer": "captain",
  "subject_id": "vm-landing-proof-20260819",
  "subject_version": 1,
  "payload": {
    "operation": "hermes.plan",
    "project_id": "vm-landing-proof-20260819",
    "prompt_ref": {
      "uri": "artifact://sha256/e774a26dd7216cc7447c379907d08678b0beece18e285f2add71101dcefe2eeb",
      "sha256": "e774a26dd7216cc7447c379907d08678b0beece18e285f2add71101dcefe2eeb",
      "media_type": "text/plain"
    },
    "capability_profile": "planner",
    "limits": { "wall_seconds": 60, "max_iterations": 1 },
    "integration_intent": "none"
  }
}
```

Raw response (verbatim):

```
HTTP 503
{"detail":"runtime execution failed"}
```

Confirmed from the live runtime's own access log (`runtime-stdout.log`,
read-only, not modified):
`INFO: 127.0.0.1:24954 - "POST /v1/runtime/execute HTTP/1.1" 503 Service Unavailable`
and from `runtime-stderr.log`: `Runtime command execution failed` (the
handler logs only this fixed string, with no traceback or detail — see
§9).

**This is progress, not a dead end: the 422 shape rejection is gone.** The
request passed Pydantic validation this time.

### 8c. Attempt 2 — same command, with a `batch_id` added

Read of the source (§9) showed the 503 most likely traces to the runtime's
state port unconditionally requiring `payload.batch_id`. To test that
empirically rather than only by inference, a second attempt added a
syntactically valid but almost-certainly-nonexistent `batch_id`
(`"vm-landing-proof-20260819-batch"`) to the same payload, everything else
unchanged (fresh `event_id`/`correlation_id` per request, since the runtime
treats `event_id` as the idempotency key).

Request body sent (verbatim, only the diff from §8b's payload shown —
`batch_id` added):

```json
{
  "schema": "captain.agent-runtime-command.v1",
  "event_id": "e87eca98-6428-4e76-8896-89fc377f35ce",
  "correlation_id": "ede8f970-e355-4744-b7bb-c024f97028ea",
  "occurred_at": "2026-08-20T10:31:18.968443+00:00",
  "producer": "captain",
  "subject_id": "vm-landing-proof-20260819",
  "subject_version": 1,
  "payload": {
    "operation": "hermes.plan",
    "project_id": "vm-landing-proof-20260819",
    "prompt_ref": {
      "uri": "artifact://sha256/e774a26dd7216cc7447c379907d08678b0beece18e285f2add71101dcefe2eeb",
      "sha256": "e774a26dd7216cc7447c379907d08678b0beece18e285f2add71101dcefe2eeb",
      "media_type": "text/plain"
    },
    "capability_profile": "planner",
    "limits": { "wall_seconds": 60, "max_iterations": 1 },
    "integration_intent": "none",
    "batch_id": "vm-landing-proof-20260819-batch"
  }
}
```

Raw response (verbatim):

```
HTTP 503
{"detail":"runtime execution failed"}
```

Byte-identical to §8b's response. `runtime-stdout.log` shows a second
`"POST /v1/runtime/execute HTTP/1.1" 503 Service Unavailable` line;
`runtime-stderr.log` shows a second, identically-worded `Runtime command
execution failed` line — the server gives no client-visible or logged
signal that distinguishes *why* a 503 happened, in either attempt.

## 9. Why both direct attempts got 503 (source analysis, not a live probe)

No third live command was sent to chase this further — the two attempts
above already show the client-observable behavior is indistinguishable
between "no `batch_id`" and "a `batch_id` for a batch that doesn't exist,"
so a third empirical attempt would add no new information. What follows is
read-only analysis of already-checked-out source.

`agenten/agent_runtime/service.py`, `AgentRuntimeService.execute()`, calls
`batch = await self._state.get_released_batch(command)` **unconditionally,
for every operation**, before dispatch — this is not gated on operation
type. Only the *dispatch* call a few lines later
(`self._dispatch(command, grant)`) is wrapped in a try/except that turns
adapter failures into a structured `infrastructure_failed` result; the
`get_released_batch` call is not inside that try/except, so any exception
it raises propagates all the way up to `http_server.py`'s outer
`except Exception: raise HTTPException(503, "runtime execution failed")`.

The concrete implementation of `get_released_batch`
(`GatewayBackedRuntimeState` in `agenten/agent_runtime/runtime_entrypoint.py`)
does exactly two things that both raise:

```python
async def get_released_batch(self, command: AgentRuntimeCommand) -> WorkBatch:
    batch_id = command.payload.batch_id
    if batch_id is None:
        raise GatewayRuntimeError("runtime command has no released batch binding")
    ...
    response = await self._client.get(f"{self._base_url}/batches/{batch_id}/bundle", ...)
    if response.status_code != 200:
        raise GatewayRuntimeError(f"read released batch failed with gateway status {response.status_code}")
```

So §8b (no `batch_id`) hits the first branch; §8c (a `batch_id` for a batch
that doesn't exist) would hit the second, via a 404 from the Gateway's
`GET /batches/{id}/bundle`. Either way, `GatewayRuntimeError` is uncaught
→ 503, which is exactly what both attempts observed.

Critically, `AgentRuntimeCommandPayload`'s own contract validator
(`require_operation_contract` in `contracts.py`) only requires
`batch_id`/`subtask_id`/`workspace_ref` for the four *Codex* operations —
`hermes.plan` is explicitly exempt at the schema level. The runtime's
state-port implementation is stricter than its own request contract: it
demands a released batch for `hermes.plan` too. That gap between what the
Pydantic contract allows and what `GatewayBackedRuntimeState` actually
requires is offered as explanation, not as a confirmed defect — nothing
was changed to verify it, per instruction.

**Why no `batch_id` value could have worked here:** `GET /batches/{id}/bundle`
resolves through the Gateway's own store, which — per
`gateway/store.py:list_batches` — reads `blocks WHERE block_type =
'work_batch'`. The controller's own pre-run snapshot (§2, confirmed
independently by the controller both before round 1 and after round 1)
established `total_blocks = 0` for the entire table, not just for the
marker. With zero rows of any kind in `blocks`, no released batch can
exist anywhere in this environment right now, for any project. This
document does not query `blocks` or the Gateway's `/batches` listing
endpoint to re-confirm that emptiness directly — the controller's figure is
taken as given, exactly as instructed, and is sufficient on its own to
explain both 503s.

## 10. Where this was stopped

Making `hermes.plan` succeed from here would require a real released work
batch to exist first — a batch is created and released through Captain's
own intake/release flow (visible as `POST`-side batch/job endpoints in
`gateway/app.py`), which is a separate, larger provisioning surface, not a
request-shape or artifact-placement detail. That is a structural
precondition of the composed runtime, not something fixable by iterating
on this command's shape further. Provisioning a batch was not attempted:
it was outside the direct-command task as given, and doing it unprompted
would mean driving a materially different, unreviewed part of the system
on my own judgement. Per the coordinator's own instruction — "If you
believe [a source file] must change for a well-formed command to succeed,
stop and report — that would be a finding, not a fix" — this is reported
as a finding rather than worked around.

## 11. What round 2 proves and what it does not

**Proven independently by round 2:**
- The `origin/master` MCP server's 422 was indeed a request-shape defect
  in that server, not a property of the runtime: an envelope built exactly
  to the runtime's own declared contract clears request validation cleanly
  (no 422 in either §8b or §8c).
- The runtime's artifact port is live and correctly resolves a real,
  content-addressed artifact placed at the path its own source code
  implies (§8a) — `require()` did not reject the artifact itself in either
  attempt (had it, the failure mode read from source would differ from
  the batch-related one actually observed).
- The runtime's state layer enforces a real, load-bearing precondition
  (an already-released work batch) before it will run *any* operation,
  `hermes.plan` included, and fails closed (503, no partial effect) when
  that precondition cannot be met — consistent with the controller's
  independent finding after round 1 that the ledger was left in exactly
  its pre-run state.

**Not proven by round 2:**
- That `hermes.plan`, `codex.run`, or `agentfarm.deliver` can succeed
  end-to-end through this composed runtime. Neither attempt reached
  Hermes/Codex adapter dispatch or any ledger write.
- No row is expected to exist for marker `vm-landing-proof-20260819` after
  round 2 either — both attempts failed before the persistence layer, for
  the reasons in §9. The controller's post-round-2 query is expected to
  again show the marker absent, consistent with (not a gap in) the query.

## Ledger proof (controller)

*(Left empty — the controller fills this in with the raw SQL output from
its own Step 4 query, per the spec's rule that only a query the controller
runs itself, against its own command output, counts as evidence.)*

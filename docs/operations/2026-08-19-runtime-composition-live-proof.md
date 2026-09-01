# Runtime-composition live proof — `agentfarm.deliver` via MCP (2026-08-19)

> **Update (2026-08-20, round 3):** rounds 1-2 below ended in refusals and
> recorded no ledger write. **Round 3 got through**: a fully-bound,
> contract-valid command reached real Hermes adapter dispatch, and the full
> chain (batch -> command -> grant -> result) is now persisted in the ledger.
> The ledger proof this document originally lacked is in **Round 3** and
> **Ledger proof (coordinator)** below. Read the rounds 1-2 summary that
> follows as the state at the time it was written.

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

## Round 3 (2026-08-20) — the composed runtime reached real Hermes dispatch and wrote the full chain to the ledger

Round 3 was run by the coordinator directly (no split agent), so every
figure below comes from a command or query run in this session.

### 3.1 The environment had to be rebuilt first

`mariadb-test` was found `Exited (255)`, and ports 8090/8091 were dead. The
test database mounts `/var/lib/mysql` as **tmpfs** (see
`docker-compose.test.yml`), so restarting it produced an *empty* schema.
That is why the round 1/2 finding "`blocks` is completely empty" no longer
had to be taken on trust — it was re-established from zero.

Note for future runs: the compose project name `captain-cook-test` is fixed
in the compose file and is therefore **shared across worktrees**. Running
`docker compose up` from this worktree adopted a container whose labels
still record `working_dir=...\.worktrees\hermes-factory-c`. Same image,
ports and env, but two worktrees can collide on this one container.

### 3.2 A regression in this branch's own start script blocked the managed path

`scripts/live-demo-services.ps1 start` failed with `Runtime did not become
healthy.` while `runtime-stdout.log` showed the runtime answering
**`GET /health -> 200 OK` fifteen times**. The health probe was never the
problem.

Cause, reproduced empirically rather than inferred: `Start-Runtime` launched
`.venv\Scripts\python.exe` (PID 37156), but that venv stub is a pyenv-win
redirector which re-execs the real interpreter
(`~\.pyenv\pyenv-win\versions\3.11.0\python.exe`, PID 43016) as a **child**.
The socket is bound by the child, so `Assert-ManagedRuntimeListener`, which
compared the listener owner against the *launcher* PID, could never pass on
this machine:

```
healthProbe200=True afterAttempts=1
listenerCount=1
  owningPID=43016  procName=python
managedPID=37156  ownershipMatches=False
```

`git log -L` attributes that assertion to **8e95210**, a commit on this
branch: `Start-Gateway` was ported to the listener-identity pattern and
`Start-Runtime` was not. Because the assertion ran inside `try { } catch {}`,
its exception was swallowed and the loop simply timed out, reporting a
misleading "not healthy" message that contradicted the logs.

Fixed here by porting `Start-Runtime` to the pattern `Start-Gateway` already
uses: resolve the listener-owning process, require it to be the launcher
**or an exact child of it** running the expected
`-m agenten.agent_runtime.runtime_entrypoint` module, and record *that*
process in the identity file. The swallowed error is now surfaced in the
failure message. `tests/test_live_demo_operations.py`: **14 passed**.
Result: `[ready] authenticated Runtime boundary with verified process identity`.

Health matrix after the fix (reproducing section 1):
`GET :8090/healthz -> 200 {"status":"ok","database":"ready"}`,
`GET :8091/health` with bearer `-> 200 {"status":"ok"}`, without `-> 401`.

### 3.3 Pre-run ledger snapshot (run by the coordinator, independent)

```
total_blocks            0
preexisting_marker_rows 0
```

### 3.4 The released batch was provisioned through Captain's own endpoint

`POST /blocks` (captain role) with `block_type: work_batch` and
`batch_id: vm-landing-proof-20260819` — the marker doubles as the batch
identity, which fits `WorkBatch.batch_id`'s own pattern
`^[a-z0-9][a-z0-9-]{0,31}$` (25 characters). Result: **HTTP 201**, block
index 0. `GET /batches/vm-landing-proof-20260819/bundle` then returned
**HTTP 200** — the precondition that produced round 2's 503 was satisfied.

### 3.5 Three further fail-closed layers, each found by measurement

Provisioning the batch did not make `hermes.plan` succeed. Three more
refusals followed, each isolated to a verbatim response:

1. **`HTTP 409 {"detail":"runtime subtask was not released in the batch"}`**
   — isolated by posting the command straight to `POST /v1/runtime/commands`,
   with no side effect. When `batch_id` is present,
   `store.py:_accept_runtime_command_once` requires `payload.subtask_id` to
   be one of the batch's `subtask_ids`; `hermes.plan` carries no
   `subtask_id`, and `None` is never in that list. The runtime surfaced this
   only as an opaque 503.

2. **`HTTP 422 {"detail":"invalid runtime command"}`** — adding `subtask_id`
   alone is rejected by the envelope, whose validator states `subject_id
   must match payload.subtask_id`. A subtask-bound command must therefore
   also move `subject_id` from the project id to the subtask id.

3. **`CapabilityDenied: runtime grants require batch, subtask, and workspace
   bindings`** (surfaced as another opaque 503) —
   `capabilities.py:derive_grant` requires all three bindings for *any*
   operation.

This is the same gap round 2 named, appearing three more times: the Pydantic
contract exempts `hermes.plan` from the batch/subtask/workspace trio, while
the accept path, the envelope rule and the capability layer each require it
in practice. **No source was changed to get past any of them** — the
contract permits those fields on `hermes.plan`, so a fully-bound,
contract-valid command satisfies all three.

### 3.6 The fully-bound command dispatched for real

Same envelope as 8b plus `batch_id`, `subtask_id`, `workspace_ref:
workspace://vm-landing-proof-20260819`, and `subject_id` set to the subtask
id. Response:

```
HTTP 200
{
  "schema": "captain.agent-runtime-result.v1",
  "command_id": "72190450-b992-4648-b38b-80bc915f89e9",
  "subject_id": "vm-landing-proof-20260819-subtask-1",
  "grant_id": "grant-9d713d996e37f736eccbe29c0df74665",
  "operation": "hermes.plan",
  "status": "infrastructure_failed",
  "evidence_refs": [ { "uri": "artifact://sha256/f4fd7983ad4892ab9e3ba6b245f9baa449bd17ec37e701303ce06376b18e14b5", ... } ],
  "error": "hermes.plan execution failed"
}
```

A capability grant was issued, the adapter was dispatched, and the failure
came back as a **structured result**, not an exception — the
`infrastructure_failed` path round 2 predicted from source reading, now
observed.

Evidence artifact, verbatim:

```json
{"command_id":"72190450-b992-4648-b38b-80bc915f89e9","correlation_id":"dac82f26-2317-43c7-a2a4-3fec3ed1ef74","failure_id":"ef6e100b-bc58-51c4-a61a-6c253691248d","occurred_at":"2026-08-20T13:15:00.110878Z","operation":"hermes.plan","reason_code":"adapter_failed","schema":"captain.runtime-infrastructure-failure-evidence.v1","status":"infrastructure_failed"}
```

### 3.7 Why Hermes itself failed — traced to a boundary, not guessed

`reason_code: adapter_failed` carries no detail, and nothing is logged, so
the adapter call was reproduced in-process to obtain the real exception:

```
json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
  ... hermes_cli.py:4036 in _parse_evidence_payload
The above exception was the direct cause of:
agenten.agent_factory.orchestration.FactoryDispatchError:
  Hermes must return exactly one typed runtime plan
  ... captain_production_adapters.py:200 in _invoke
```

The adapter loaded is the digest-verified `CaptainHermesPlannerAdapter`. It
spawned the Hermes CLI, which **returned empty/non-JSON stdout**, and the
adapter refused to fabricate a plan.

Checked, so that it is not guessed at: the CLI is installed and runnable
(`hermes --version` -> `Hermes Agent v0.18.2 (2026.7.7.2)`), and
`OPENAI_API_KEY` **is** present and non-empty in the environment. So this is
*not* a missing-binary or missing-credential failure. Why the Hermes CLI
emitted nothing for this runtime-plan envelope lies inside the Hermes agent
subsystem; diagnosing that means driving a materially different subsystem,
which was out of scope here and is reported rather than pursued.

### 3.8 Observability findings (unfixed, reported)

- `http_server.py` logs `logger.error("Runtime command execution failed")`
  with **no `exc_info`**, so every distinct cause collapses to one
  indistinguishable line plus a 503. All three refusals in 3.5 were
  externally identical. This is what forced round 2 into source reading and
  round 3 into in-process reproduction.
- `reason_code: adapter_failed` records no underlying adapter error, and the
  Hermes subprocess's stderr is not surfaced with the failure.

Neither was changed — both are behavioural changes to the runtime, and the
standing instruction is to report such things as findings.

## Ledger proof (coordinator)

Query run by the coordinator against its own commands' output, after the run
in 3.6:

```
total_blocks   5
marker_rows    5

index  block_type             status                 event_id                              operation    hash16            prev16
0      work_batch             pending                NULL                                  NULL         d9c4127520ae713c  0
1      agent_runtime_command  accepted               9134b493-fca6-4dee-bf95-eb4b29320a3a  hermes.plan  0ed15c81a25baade  d9c4127520ae713c
2      agent_runtime_command  accepted               72190450-b992-4648-b38b-80bc915f89e9  hermes.plan  c0e062ebdc455004  0ed15c81a25baade
3      agent_runtime_grant    active                 NULL                                  NULL         0bfc5d4875651566  c0e062ebdc455004
4      agent_runtime_result   infrastructure_failed  7fb784cf-607b-5816-97d6-e2ace3f49ecd  NULL         6627a1c63a7f6973  0bfc5d4875651566
```

**This is the end-to-end ledger proof the earlier rounds could not produce.**
The whole chain persisted — batch -> command -> grant -> result — every row
carries the marker, and each `previous_hash` equals its predecessor's `hash`,
so the chain is intact and linear.

Block 1 is worth keeping in view rather than tidying away: it is the
subtask-bound command that was **accepted and persisted but never granted**,
because it lacked `workspace_ref` (3.5, item 3). The ledger honestly records
an accepted command that produced no grant and no result.

### Cleanup status — deliberately not yet performed

The marker rows are the evidence above, so they were not deleted on sight.
Two things bear on how this should be cleaned up:

- The rows are hash-chained. Deleting rows 0-4 individually would leave the
  chain dangling rather than clean; this ledger is append-only by design.
- The database is **tmpfs-backed and ephemeral**: tearing down
  `captain-cook-test-mariadb-test-1` discards all five rows with no chain
  damage, which is the natural cleanup for this environment.

Left running so the proof stays inspectable. Teardown is a one-command step
once the evidence has been reviewed.

## Ledger proof (controller, rounds 1-2)

*(Left empty — the controller fills this in with the raw SQL output from
its own Step 4 query, per the spec's rule that only a query the controller
runs itself, against its own command output, counts as evidence.)*

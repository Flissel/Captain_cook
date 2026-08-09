# Captain Responses budget proxy

This proxy is the provider-receipt authority for one already-authorized
`codex.resume` effect. It listens only on `127.0.0.1`, exposes the Responses
create/read/cancel surface, and is the only process in this path that receives
both `OPENAI_API_KEY` and the distinct Gateway provider-receipt credential.
Codex receives only `CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN` through an isolated
`CODEX_HOME`.

The price card in `config/openai-price-card-terra-2026-08-09.json` records the
official live values observed on 2026-08-09: 2.00 USD per million input tokens,
0.20 USD per million cached input tokens, and 12.00 USD per million output
tokens. It also records the 272,000-token tier boundary and 1.25x cache-write
multiplier. The proxy rejects inputs at or above that boundary, so the large
context multipliers can never become ambiguous. Replace the card after expiry;
never extend its expiry without checking the official source.

References:

- https://developers.openai.com/api/docs/models/gpt-5.6-terra
- https://developers.openai.com/api/docs/guides/token-counting
- https://developers.openai.com/api/docs/guides/background
- https://learn.chatgpt.com/docs/config-file/config-reference

## Authority and cost sequence

1. Captain/Gateway creates and validates the D1 resume cost authority.
2. The operator writes a gitignored `captain.provider-budget-context.v1` bound
   to the exact command, original command, deterministic runtime result,
   reservation, job, run, capability, prior Codex session, ceiling, and expiry.
3. Before every provider effect, the proxy calls the official Responses input
   token count endpoint with the same model input, tools, and schemas.
4. SQLite `BEGIN IMMEDIATE` atomically reserves the conservative worst case.
   The proxy derives `max_output_tokens` from the remaining run ceiling.
5. The proxy forwards only `gpt-5.6-terra`, reasoning `high`, known non-hosted
   Codex tools, and zero transport retries.
6. Every terminal provider call is independently repriced and stored as a
   raw-free append-only call receipt. After Codex itself is terminal, the
   Runtime calls the authenticated proxy finalize route with only the exact
   command and deterministic result IDs. The proxy verifies every call is
   terminal, aggregates the immutable usage, and submits exactly one D1
   provider settlement with its separate credential.
7. Missing or ambiguous usage, disconnect, cancellation without usage, and
   settlement failure consume the full reservation and cannot produce a
   Gateway provider settlement.

Background response readback and cancellation are idempotent. Raw provider
output is streamed to the authenticated local Codex client but is never stored
by the budget repository or CAS.

## Live preflight

No paid provider call is part of the offline test suite. Before an approved
live run:

1. Generate the exact D1 context from the persisted resume command and its
   prior Codex checkpoint. `session_id` must equal that prior checkpoint's
   session because the runtime resumes the same Codex thread.
2. Configure the Gateway with the `RuntimeUsagePricingSnapshotV1` derived from
   the active price card (same snapshot ID, rates, effective/expiry times, and
   computed digest).
3. Export three distinct process-only values:
   `OPENAI_API_KEY`, `CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN`, and
   `CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN`. They must also differ from Captain,
   Worker, and Runtime credentials.
4. Start the Gateway and verify the D1 command and grant exist before starting
   the proxy.
5. Start the proxy and create an isolated Codex home:

   ```powershell
   .\.venv\Scripts\python.exe scripts\run_provider_budget_proxy.py `
     --context .captain-cook\provider-budget-context.json `
     --price-card config\openai-price-card-terra-2026-08-09.json `
     --policy config\provider-budget-policy.json `
     --state-root .captain-cook\provider-budget-proxy `
     --gateway-url http://127.0.0.1:8090 `
     --codex-home .captain-cook\codex-provider-home
   ```

6. Set `CAPTAIN_PROVIDER_PROXY_URL=http://127.0.0.1:18091` on the Runtime, then
   launch Codex with `CODEX_HOME` set to that generated directory and only the
   proxy client token in its environment. Explicitly remove `OPENAI_API_KEY`
   and the Gateway provider token from the Codex child environment.
7. The Runtime must complete proxy finalization before it posts the D1 cost
   settlement or returns the Runtime result. Require Gateway readback of the exact provider settlement,
   runtime result, and cost settlement before any success claim.

This implementation is offline-tested provider infrastructure. It is not live
OpenAI, paid-call, or production-release evidence by itself.

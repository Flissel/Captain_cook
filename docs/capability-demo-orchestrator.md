# Capability demo operator lifecycle

D12 is the fail-closed operator surface for the three canonical capability inputs:

1. `demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md`
2. `demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md`
3. `demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md`

It exposes `init`, `start`, `status`, `run`, `resume`, and `stop` through:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 <action>
```

## Current honest capability boundary

`init` is usable now. It atomically creates only missing local authorities in the gitignored `.captain-cook/live-demo/private/demo.env`. It never creates, copies, overwrites, or prints `OPENAI_API_KEY`.

`stop` is usable independently. It targets only persisted PID plus process-start identities, verifies termination, and never removes Docker volumes.

The remaining actions bind to D11's real `captain.capability-production-bundle-manifest.v1`. D12 does not define a second manifest, guess commands, or manufacture endpoint templates. D11 currently exports authenticated Gateway, benchmark, Runtime, dynamic provider-proxy, and Minibook health topology. Its provider mode is `dynamic_gateway_context_v1`; every invocation uses the opaque Gateway-issued `X-Captain-Provider-Context-Ref`. A fixed provider budget context is never shared across the three inputs.

D11 does not yet export operator process start/restart commands. Therefore `start` and `status` exit `2` with exactly:

```text
D11 operator process bootstrap is unavailable
```

D11 also does not expose exact assembly-evidence readback from Minibook. Therefore `run` and `resume` fail before HTTP, Gateway advances, provider effects, or checkpoint mutation with exactly:

```text
D11 Minibook assembly evidence readback capability is unavailable
```

These are readiness blockers, not successful live-demo claims.

## Paid-run contract

When the missing D11 capabilities exist, a paid action still requires explicit authority and has a hard aggregate ceiling:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 run -AllowPaidRun -MaxCostUsd 6.00
```

Before submission, D12 validates the D10 private catalog, process/restart capability, Minibook readback, all bearer-token values, every authenticated D11 health binding, and the real Captain assembly health route. Missing credentials cause zero HTTP calls and zero advances. The three requests have distinct deterministic request, correlation, and reservation identities; lifecycle receipts remain Captain/Gateway-issued. Each advance uses `maximum_slices=1`.

Timeouts remain exit `124`, including service health, HTTP, and restart waits. Configuration/authority gaps use exit `2`. Spawned PIDs are persisted before health/listener checks; a failed start or restart terminates the exact process tree and verifies it is gone. Listener ownership accepts loopback and IPv4/IPv6 wildcard bindings but requires one exact owning PID.

## Non-claims

The test suite uses injected process and HTTP boundaries. No Docker, provider, database, n8n, Gateway, Runtime, benchmark, or Minibook live action is performed by ordinary verification. A live claim requires the missing D11 process-control and Minibook-readback exports plus an explicitly authorized operator run.

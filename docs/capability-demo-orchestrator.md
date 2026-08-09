# Capability demo operator lifecycle

The D12 lifecycle is the supported operator surface for the three-pattern live demo. It uses the Captain capability-assembly API and never accepts a caller-selected conversation pattern. Captain compiles these repository inputs in the fixed product order:

1. `demo_inputs/agent_factory/sales_pipeline_brief/TO_BE_BUILT.md`
2. `demo_inputs/agent_factory/incident_command/TO_BE_BUILT.md`
3. `demo_inputs/agent_factory/claims_resolution/TO_BE_BUILT.md`

The old `run-capability-live-demo.ps1` command is now only a compatibility shim. Its `start` action forwards to `capability-demo.ps1 run`; its `verify` action forwards to authenticated service status. It contains no example input paths and sends no `--pattern` argument.

## Prerequisites

- Python 3.11 and the project development environment are installed.
- Docker Desktop is available. D12 validates every sealed Compose file with `docker compose config --quiet` before `up -d`.
- The operator places `OPENAI_API_KEY` in the gitignored root `.env`. `init` does not create, copy, display, or overwrite that key.
- D10 has materialized the private benchmark catalog below `.captain-cook/live-demo/private/`.
- D11 has materialized a `captain.capability-demo-bundle.v1` manifest and written only its path and SHA-256 to the private demo environment as `CAPTAIN_DEMO_BUNDLE_MANIFEST` and `CAPTAIN_DEMO_BUNDLE_MANIFEST_SHA256`.

The sealed D11 bundle binds the exact Git revision, three canonical input digests, process artifact digests, Compose digests/project, D10 catalog digest, Gateway URL, Minibook readback URL, and exactly four services in this dependency order:

```text
gateway -> provider_proxy -> runtime -> benchmark
```

Each service supplies a loopback listener, an authenticated health URL on that listener, an argv array, working directory, environment allowlist, and digest-bound artifact paths. `OPENAI_API_KEY` is required in the provider-proxy allowlist and forbidden from Gateway, Runtime, and benchmark environments.

## Commands

Initialize only missing private demo authorities:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 init
```

This writes `.captain-cook/live-demo/private/demo.env` atomically. It never changes an existing root `.env`, never replaces an existing non-empty value, and prints no credential value.

Validate the full sealed configuration and start/reuse the service chain:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 start
```

All manifests, digests, commands, Compose files, ports, existing PID/start identities, and reusable health endpoints are checked before the first Docker or process mutation. Reuse requires the exact command/config hash, live PID with the same process-start identity, matching listener owner, and authenticated health.

Inspect authenticated status without changing service state:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 status
```

From a ready service state, the exact paid live-demo command is:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 run -AllowPaidRun -MaxCostUsd 6.00
```

The total ceiling is at most USD 6.00 and is divided into three positive reservations whose sum is exact. Each API advance carries `maximum_slices=1`. The client persists the Gateway checkpoint after every slice, restarts only the managed Runtime immediately after Captain returns the `resume_authorize` slice, and continues the same assembly and correlation identities. Final success requires byte-canonical equality between Gateway final evidence and Minibook readback.

Resume an interrupted run without creating another submit request:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 resume -AllowPaidRun -MaxCostUsd 6.00
```

HTTP or lifecycle timeout remains process exit code `124` and is recorded in `.captain-cook/live-demo/current-run.json`; it is not rewritten as empty output. Exact operation identities make an ambiguous one-slice retry idempotent at the Captain API boundary.

Stop only the PID/start identities recorded by D12:

```powershell
pwsh -NoProfile -File scripts/capability-demo.ps1 stop
```

`stop` does not require the current D10/D11 bundle, does not terminate an identity whose process start changed, and never removes or prunes Docker volumes.

## Evidence and non-claims

Mutable state, logs, private configuration, checkpoints, and final evidence remain under the gitignored `.captain-cook/live-demo/` directory. Console progress contains only schema, status, service, slice count, and assembly identity fields.

The deterministic D12 tests use injected process and HTTP boundaries. They prove ordering, authorization, digest binding, restart/resume behavior, redaction, cost gating, timeout preservation, and evidence comparison. They do not prove that Docker, OpenAI, MariaDB, n8n, Gateway, Runtime, benchmark, or Minibook ran live. A real live claim begins only after D10 and D11 provide their sealed artifacts and the operator explicitly runs the paid command above.

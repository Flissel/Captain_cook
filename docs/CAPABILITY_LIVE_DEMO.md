# Capability Factory live demo

This runbook drives three distinct canonical `TO_BE_BUILT.md` inputs through
the provider-backed capability factory. It requires a controlled recovery and
three E2E evidence traces per new capability, restarts only Captain-owned demo
services, resumes the first correlation without changing its execution
identity, and retains a redacted Gateway/Minibook evidence summary.

## Safety boundary

- The default invocation validates the manifest and exits without provider
  work. Live work requires both `-LiveProviders` and `-ConfirmProviderCost`.
- `-MaxCostUsdPerInput` is a per-input provider ceiling. The default three
  inputs can therefore consume at most three times this value.
- All mutable delivery state is isolated to `captain_test` and paths below the
  gitignored `.captain-cook/` directory. No volume is removed.
- The service lifecycle uses only `scripts/live-demo-services.ps1`; VibeMind
  services and volumes remain outside its authority.
- The generated evidence contains stable IDs, hashes, statuses, durations,
  and the commit only. Credentials and provider response bodies are excluded.

## 1. Validate without provider calls

```powershell
pwsh -NoProfile -File scripts/run-capability-live-demo.ps1
```

The default set covers sequential, reflection/retry, and tool-led n8n
conversation patterns. Select any other three distinct patterns from
`demo_inputs/agent_factory/manifest.json` with `-InputIds`.

## 2. Run the provider-backed demo

```powershell
pwsh -NoProfile -File scripts/run-capability-live-demo.ps1 `
  -LiveProviders `
  -ConfirmProviderCost `
  -MaxCostUsdPerInput 1.00
```

The script leaves Captain-owned services running after success so the Gateway,
Runtime, Minibook, and Captain n8n views remain available for the demonstration.
It writes one redacted report to
`.captain-cook/evidence/capability-live-demo-<timestamp>.json`.

## 3. Optional named-window recording

Recording is opt-in and requires ffmpeg plus the exact title of a dedicated,
secret-free demo window. Capture is limited to that named window.

```powershell
pwsh -NoProfile -File scripts/run-capability-live-demo.ps1 `
  -LiveProviders `
  -ConfirmProviderCost `
  -MaxCostUsdPerInput 1.00 `
  -RecordVideo `
  -RecordingWindowTitle 'Captain Cook Live Demo'
```

Before recording, open a dedicated window with that title and ensure it shows
only the redacted demo output. The MP4 is stored beside the JSON evidence.

## Success criteria

The command fails closed unless all of these are true:

1. Three tracked inputs have different AutoGen conversation patterns and
   different correlation IDs.
2. Every new capability reaches `ready_to_use`, has completed Gateway
   execution, no unresolved required tool gap, and at least one correlated
   Minibook projection event.
3. Creation evidence contains one controlled recovery, three distinct normal
   E2E batch IDs, and four release-evidence digests.
4. After a Captain-only service restart, replaying the first correlation uses
   released authority and preserves capability, command, result, and
   projection identities.

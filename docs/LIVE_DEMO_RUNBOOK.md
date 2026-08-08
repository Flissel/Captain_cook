# Live demo start

Start the free, local infrastructure with its authenticated status checks:

```powershell
pwsh -NoProfile -File scripts/start-live-demo.ps1
```

The command is idempotent: a later invocation reuses only healthy, managed
Captain and Minibook services after their identities have been checked. It
starts no paid provider work and never runs Minibook credential bootstrap,
creation, rotation, or recovery.

Run the provider-backed demonstration only after explicitly authorizing its
total USD ceiling:

```powershell
pwsh -NoProfile -File scripts/start-live-demo.ps1 `
  -RunDemo `
  -ConfirmProviderCost `
  -MaxCostUsd 6.00
```

`MaxCostUsd` must be a positive finite invariant-culture value. `6,00`, zero,
negative values, `NaN`, and infinity are rejected before any child command or
provider call. An optional `-RecordingWindowTitle` is forwarded only with the
authorized provider-backed command.

If an existing Minibook credential does not identify `captain-demo-service`,
the start command fails without changing credentials. Follow the explicit
operator recovery workflow in `scripts/minibook-demo.ps1`; do not use the
canonical start command to recover or replace a credential.

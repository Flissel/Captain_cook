# D7 provider budget proxy review round 1

Reviewed range: `8dddd4c..ac282eb` on `codex/provider-budget-proxy-d7`.

## Authority and cost findings

- Resolved: Gateway previously returned `hard_ceiling_enforced=false`
  unconditionally. It now returns true only for a complete command binding
  whose loopback URL, provider-policy digest, and price-card digest equal the
  active Gateway configuration. Missing or changed bindings remain false.
- Resolved: the provider context was local input only. The proxy now performs
  authenticated exact Gateway readback before token counting, after counting
  immediately before reservation/provider dispatch, and before read, cancel,
  and finalize operations. External failures use one redacted error.
- Resolved: a slow token count may cross the authority expiry, but then creates
  neither a local reservation nor an OpenAI response effect.
- Resolved: provider call terminal timestamps and aggregate finalization must
  remain strictly inside the authority expiry. Ambiguous terminal usage still
  consumes the full call reservation fail closed.
- Resolved: Codex previously retained a direct OpenAI credential lane and its
  launcher ignored the generated proxy configuration. Proxy-bound resumes now
  receive immutable CLI overrides for the exact loopback Responses provider;
  the child retains only the proxy client credential and strips OpenAI,
  Gateway, provider-receipt, and Runtime secrets.
- Resolved: production resume adapters no longer downgrade an exact
  authenticated proxy-bound execution to `policy_failed`. Unbound legacy
  executions remain fail closed.

## Verification

- Focused provider/Gateway/Runtime suite: 317 passed.
- Architecture/import/workstream suite: 18 passed.
- `compileall`: passed.
- Submission evidence check: passed.
- Diff whitespace check: passed; worktree clean after this report commit.

No network, paid provider, Docker, or database effect was used. This review is
not live-provider evidence. Remaining live preflight is to configure matching
Gateway policy/price digests, mint distinct process credentials, generate the
exact persisted context, and perform one explicitly approved call while
checking provider settlement precedes Runtime result persistence.

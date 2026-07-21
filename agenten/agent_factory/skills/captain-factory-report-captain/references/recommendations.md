# Approved Recommendations

Choose exactly one: `PROMOTE_CANDIDATE`, `RETRY_BUILD`, `BLOCKED_TOOL_REQUIRED`,
`BLOCKED_CREDENTIAL_REQUIRED`, `BLOCKED_INFRASTRUCTURE`, `BUDGET_EXHAUSTED`, or
`MANUAL_DECISION_REQUIRED`. Bind the recommendation to evidence digests,
assertion identifiers, and gap identifiers. Captain alone recomputes the next
lifecycle transition and promotion decision.

Classify gaps without inference:

- A required TODO_TOOL.v1 always requires `BLOCKED_TOOL_REQUIRED` and stops
  paid execution. A self-built adapter remains a required unresolved tool and
  uses the same classification until Captain validates, publishes, and releases
  it with its acceptance test.
- An optional TODO_TOOL.v1 permits `PROMOTE_CANDIDATE` only when every released assertion is green
  without that capability; otherwise use the classification supported by the
  failed evidence.
- An existing tool that lacks an externally supplied credential requires
  `BLOCKED_CREDENTIAL_REQUIRED`; record only the credential reference.
- Implemented code whose leased service is unavailable requires
  `BLOCKED_INFRASTRUCTURE`.

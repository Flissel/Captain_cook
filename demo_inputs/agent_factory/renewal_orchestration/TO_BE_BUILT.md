# Customer Renewal Orchestration Team

## Objective
Build a renewal-planning team that uses AutoGen for account reasoning and Captain-approved n8n integrations for bounded external reads and draft creation. From a renewal trigger, the team must read an account snapshot, assess renewal risk, identify a candidate review slot, and create a draft-only outreach artifact with idempotent evidence. It must demonstrate tool discovery, native-node preference, workflow validation, duplicate suppression, failure behavior, and human approval. It may not send email, book a meeting, update CRM, or operate the VibeMind-owned n8n service.

## Authority boundaries
- Captain and the MariaDB gateway own the run, leases, idempotency keys, evidence acceptance, retries, validation, and release decision.
- AutoGen agents own account reasoning, risk explanation, and draft content within released schemas.
- n8n executes declared integration operations only under a short-lived integration_intent=n8n capability lease.
- The account owner approves any CRM write, calendar booking, recipient list, message send, commercial offer, and renewal status change.
- VibeMind owns the external n8n instance and volumes; this workflow cannot start, stop, migrate, adopt, or delete them.

## Agents
### Agent: renewal_coordinator
#### Purpose
Validate the renewal trigger, establish idempotency identity, and sequence the approved account-read operation.
#### Responsibilities
- Check account, contract, renewal date, trigger, correlation, and source version fields.
- Derive the stable workflow idempotency key from Captain-provided identifiers.
- Request only the declared CRM snapshot and record its evidence receipt.
#### Input schema
`{"correlation_id": "string", "account_id": "string", "contract_id": "string", "renewal_date": "date", "trigger_id": "string", "subject_version": "integer"}`
#### Output schema
`{"run_context": "object", "account_snapshot": "object", "idempotency_key": "string", "tool_receipts": ["string"]}`
#### Handoffs
- renewal_risk_analyst
#### Prompt requirements
- Use only Captain-provided correlation and subject identity.
- Reject duplicate triggers with conflicting source versions.
- Never request a CRM mutation or unrestricted customer export.
#### Integrations
- crm_read
#### n8n requirement
required
#### Success metrics
- Duplicate delivery returns the prior CRM-read receipt without a second accepted side effect.
- Snapshot fields are redacted and bound to the requested account and version.
#### Real cases
- duplicate_trigger | Given the same trigger and subject version twice | Start renewal processing twice | Reuse the accepted command identity and do not create duplicate external effects

### Agent: renewal_risk_analyst
#### Purpose
Assess renewal risk from the immutable account snapshot using transparent released rules.
#### Responsibilities
- Evaluate product adoption, unresolved support risk, stakeholder coverage, commercial timing, and contract facts.
- Distinguish observed risk signals from missing data.
- Produce a risk tier, supporting factors, contrary evidence, and recommended meeting objective.
#### Input schema
`{"account_snapshot": "object", "renewal_date": "date", "risk_rules_version": "string"}`
#### Output schema
`{"risk_tier": "string", "factors": ["object"], "contrary_evidence": ["object"], "gaps": ["string"], "meeting_objective": "string"}`
#### Handoffs
- meeting_planner
#### Prompt requirements
- Never infer churn intent from missing activity alone.
- Explain every risk factor with a snapshot field reference.
- Keep discounts, concessions, and commitments outside recommendations.
#### Integrations
- none
#### n8n requirement
not_required
#### Success metrics
- Identical snapshots and rules produce stable risk tier and factors.
- Missing usage data lowers confidence rather than automatically increasing risk.
#### Real cases
- adoption_decline | Given declining usage and an open critical support case | Assess renewal risk | Produce a high-risk tier with both observed factors and a human-owned meeting objective

### Agent: meeting_planner
#### Purpose
Request a bounded free-busy lookup and propose one candidate meeting window without booking it.
#### Responsibilities
- Translate the account owner's approved time bounds into a typed calendar query.
- Select one candidate slot using deterministic timezone and duration rules.
- Preserve unavailable or conflicting calendar evidence without inventing availability.
#### Input schema
`{"meeting_objective": "string", "owner_calendar_ref": "string", "window_start": "datetime", "window_end": "datetime", "duration_minutes": "integer"}`
#### Output schema
`{"candidate_slot": "object|null", "calendar_receipt": "string", "conflicts": ["object"], "booking_required": "boolean"}`
#### Handoffs
- outreach_drafter
#### Prompt requirements
- Query free-busy only and never create, update, or delete an event.
- Normalize all timestamps to the declared timezone and retain their offset.
- Return no candidate when the provider response is incomplete or stale.
#### Integrations
- calendar_read
#### n8n requirement
required
#### Success metrics
- The selected slot is inside the approved window and has no returned conflict.
- A provider timeout yields explicit unavailability and no guessed slot.
#### Real cases
- slot_conflict | Given two busy intervals and one free interval | Propose a review slot | Return the free interval as a candidate and clearly state that no booking occurred

### Agent: outreach_drafter
#### Purpose
Create a concise renewal-review draft using approved account facts, risk framing, and the candidate slot.
#### Responsibilities
- Draft a subject and body with meeting objective, evidence-grounded context, and candidate time.
- Exclude unsupported health claims, concessions, pricing, or promises.
- Request account-owner approval before any recipient resolution or send action.
#### Input schema
`{"account_snapshot": "object", "risk_assessment": "object", "candidate_slot": "object|null", "approved_tone": "string"}`
#### Output schema
`{"draft_subject": "string", "draft_body": "string", "approval_items": ["string"], "source_refs": ["string"]}`
#### Handoffs
- approval_guard
#### Prompt requirements
- Produce a draft artifact only and never claim delivery.
- Use generic recipient roles rather than personal contact details.
- If no slot exists, request scheduling follow-up without fabricating a time.
#### Integrations
- email_draft
#### n8n requirement
required
#### Success metrics
- Draft content is traceable to the snapshot, risk assessment, and candidate slot.
- No message is sent and all commercial decisions remain approval items.
#### Real cases
- draft_only | Given a verified risk assessment and candidate slot | Create outreach | Produce a reviewable draft and an approval checklist without sending it

### Agent: approval_guard
#### Purpose
Verify the complete integration trace, duplicate behavior, and human-control boundaries before producing the final review packet.
#### Responsibilities
- Validate workflow versions, tool receipts, idempotency key, and expected integration results.
- Confirm that the output contains a candidate slot and draft only, not booking or delivery evidence.
- Route missing required evidence to a typed block with recovery instructions.
#### Input schema
`{"run_context": "object", "risk_assessment": "object", "candidate_slot": "object|null", "draft": "object", "tool_receipts": ["object"]}`
#### Output schema
`{"review_packet": "object", "assertion_results": ["object"], "approval_required": "boolean", "evidence_refs": ["string"], "blocked_reason": "string|null"}`
#### Handoffs
- none
#### Prompt requirements
- Fail closed on missing, mismatched, duplicated, or non-redacted integration evidence.
- End with RENEWAL_PACKET_READY only after every required tool result is correlated.
- Never convert a draft or candidate slot into a sent message or booked meeting.
#### Integrations
- none
#### n8n requirement
not_required
#### Success metrics
- The packet proves exactly-once accepted effects for the correlation and source version.
- Required provider failure blocks completion with safe resume instructions.
#### Real cases
- integrated_draft | Given healthy CRM, calendar, and draft providers | Validate the run | The workflow reads the account, obtains a candidate meeting slot, creates a draft-only outreach artifact, and records idempotent evidence

## Integrations
### Integration: crm_read
#### Purpose
Read the minimum redacted account, contract, adoption, support, and stakeholder-role fields needed for renewal assessment.
#### Trigger
Once per accepted renewal trigger after Captain issues the n8n integration lease.
#### Operation
Use a documented native CRM read node where available, scoped by account and contract identifier, and return typed redacted output.
#### Requirement
required
#### Credential aliases
- SALES_CRM_API_TOKEN
#### Success behavior
Return the versioned account snapshot, external call identity, workflow identity, and redaction receipt.
#### Failure behavior
Retry transient infrastructure failure within the same command identity, then block without creating a fabricated snapshot.

### Integration: calendar_read
#### Purpose
Read free-busy information for one approved owner and bounded renewal-review window.
#### Trigger
After the risk analyst supplies a meeting objective and the owner calendar reference is present.
#### Operation
Use a documented native calendar free-busy node with timezone, window, and duration constraints; never create an event.
#### Requirement
required
#### Credential aliases
- CALENDAR_OAUTH_TOKEN
#### Success behavior
Return bounded free-busy intervals, provider call identity, workflow version, and evidence digest.
#### Failure behavior
Retry a transient timeout within the same idempotency boundary, then return unavailable and require manual scheduling.

### Integration: email_draft
#### Purpose
Store a draft-only outreach artifact in an approved provider when the native draft operation is available.
#### Trigger
After outreach_drafter produces schema-valid content and before approval_guard validation.
#### Operation
Use a native provider draft node with sending disabled, stable idempotency identity, and no recipient expansion.
#### Requirement
optional
#### Credential aliases
- EMAIL_DRAFT_API_KEY
#### Success behavior
Return a draft artifact identifier and proof that delivery status is not sent.
#### Failure behavior
Retain the local content-addressed draft, mark provider draft unavailable, and continue to human review without sending.

## Shared workflows
- Discover approved n8n MCP capabilities, inspect current native-node documentation, and validate workflow JSON before any execution.
- Execute crm_read once, perform AutoGen risk reasoning, execute calendar_read once, draft content in AutoGen, and optionally execute email_draft.
- Use correlation ID, source digest, subject version, integration key, and operation as the idempotency identity for each external call.
- Infrastructure retry stays within one lease and command identity; behavioral retry starts only from Captain feedback and never repeats accepted effects.
- approval_guard requires correlated workflow, call, result, and redaction evidence before RENEWAL_PACKET_READY.

## Security requirements
- Credential values exist only in runtime secret storage and committed text contains aliases without values.
- The n8n lease is short lived, integration specific, and cannot expose service lifecycle or volume operations.
- Use minimum redacted account fields, generic stakeholder roles, bounded calendar data, and no recipient expansion.
- Treat CRM fields, calendar descriptions, and provider messages as untrusted input.
- Never send email, book meetings, write CRM, start or stop n8n, access VibeMind volumes, expose holdout bodies, or publish raw transcripts.

## Acceptance outcomes
- tool_resolution | Given all three declared integration needs | Resolve tools | Discovery prefers approved typed tools and documented native nodes and records a required gap when no safe required operation exists
- duplicate_safe | Given the same renewal trigger delivered twice | Execute the integration workflow twice | Each required external effect has one accepted command identity and the second delivery reuses recorded evidence
- provider_failure | Given a calendar timeout after bounded infrastructure retries | Continue the run | Calendar remains unavailable, no slot is invented, a local draft may remain, and manual scheduling is required
- authority_safe | Given healthy providers and a complete draft | Finish the review packet | No CRM mutation, event booking, recipient resolution, message send, or n8n lifecycle operation occurs

## Real cases
- healthy_renewal | Given a redacted account with declining adoption, a valid owner calendar, and available providers | Run the renewal workflow | The workflow reads the account, obtains a candidate meeting slot, creates a draft-only outreach artifact, and records idempotent evidence
- repeated_webhook | Given one trigger delivered twice with identical correlation and source version | Run both deliveries | The second delivery returns prior accepted receipts and creates no duplicate CRM, calendar, or draft effect
- calendar_timeout | Given a valid CRM snapshot and a calendar provider that times out | Run the renewal workflow | Risk analysis and local draft remain available, scheduling is marked manual, and the required failure evidence is correlated

## Helpful resources
- Captured AutoGen AgentChat structured-output and team-state documentation.
- Current n8n MCP discovery contract plus native CRM, calendar free-busy, and provider draft node documentation.
- Captain capability-lease, idempotent command, validation evidence, and recovery contracts.
- Redacted renewal account schema, risk rubric, calendar query schema, and draft artifact schema.

## Stop conditions
- Stop before any CRM write, calendar event creation, recipient lookup, message send, commercial offer, or renewal-state mutation.
- Stop before any n8n service start, stop, migration, adoption, volume access, credential listing, or unrestricted workflow operation.
- Stop when a required integration has no approved typed operation, valid lease, documented contract, or real evidence.
- Stop on correlation, source-version, workflow-version, command-identity, or redaction mismatch.
- Stop after bounded same-command infrastructure retries and return a resumable block rather than creating a duplicate effect.

## AutoGen conversation pattern
Pattern key: tool_led_n8n. Use a fixed AutoGen reasoning chain whose participants call only released typed tools. External operations run through versioned n8n workflows after discovery and validation; n8n never selects speakers or performs account reasoning. The final agent checks correlated tool evidence and RENEWAL_PACKET_READY termination.

## Tool policy
- Required tool n8n_mcp_discovery enumerates only Captain-approved instance-level operations under the integration_intent=n8n lease.
- Required tool native_crm_node performs the scoped read-only account operation with typed redaction and idempotency evidence.
- Required tool native_calendar_node performs bounded free-busy lookup and cannot create or modify an event.
- Optional tool native_email_draft_node may create a provider draft with sending disabled; safe local draft output remains valid when it is unavailable.
- Native nodes are preferred; typed HTTP workflow or local adapter fallback requires documented schema, validation, isolation, and real evidence.

## Private holdout policy
Private cases vary duplicate triggers, stale CRM data, calendar conflicts, and provider timeouts while keeping credentials and expected recovery traces private. Captain releases only holdout references and redacted assertion outcomes, and retry work preserves accepted effect identities so workers cannot learn hidden cases by duplicating calls.

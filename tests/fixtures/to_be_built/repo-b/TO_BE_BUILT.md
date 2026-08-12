# Invoice Reconciliation

## Objective
Match incoming invoices to purchase orders and flag mismatches for review.

## Authority boundaries
- Captain approves releases and external side effects.
- Agents may draft responses but may not send them.

## Agents
### Agent: triage_agent
#### Purpose
Classify each inbound request.
#### Responsibilities
- Determine topic and urgency.
#### Input schema
`{"message": "string"}`
#### Output schema
`{"topic": "string", "urgency": "string"}`
#### Handoffs
- response_agent
#### Prompt requirements
- Explain the classification using public evidence.
#### Integrations
- crm
#### n8n requirement
required
#### Success metrics
- Correctly classify the public billing case.
#### Real cases
- billing_case | Given an overdue invoice | Classify the request | billing and high urgency

### Agent: response_agent
#### Purpose
Draft a response for the classified request.
#### Responsibilities
- Produce a concise response draft.
#### Input schema
`{"topic": "string", "urgency": "string"}`
#### Output schema
`{"draft": "string"}`
#### Handoffs
- none
#### Prompt requirements
- Never claim that a message was sent.
#### Integrations
- none
#### n8n requirement
not_required
#### Success metrics
- Draft contains the next action.
#### Real cases
- response_case | Given a billing classification | Draft a response | Include the invoice review next action

## Integrations
### Integration: crm
#### Purpose
Read the customer account tier.
#### Trigger
After initial classification.
#### Operation
Read account metadata.
#### Requirement
required
#### Credential aliases
- CRM_API_KEY
#### Success behavior
Return the account tier.
#### Failure behavior
Continue with an explicit unavailable marker.

## Shared workflows
- triage_agent classifies before response_agent drafts.

## Security requirements
- Never disclose credential values.
- External writes require Captain approval.

## Acceptance outcomes
- billing_outcome | Given an overdue invoice | Run the triage workflow | The request is classified as billing and high urgency

## Real cases
- public_billing | Given an overdue invoice | Classify and draft | A billing classification and response draft are produced

## Helpful resources
- CRM field documentation: https://docs.example.invalid/crm-fields

## Stop conditions
- Stop before sending any customer communication.
- Stop when required account metadata cannot be read safely.

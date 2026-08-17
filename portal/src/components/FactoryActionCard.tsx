import { useState } from "react";
import type { FactoryActionDecision, FactoryActionRequest, FactoryActionResponseDraft } from "../types";

interface FactoryActionCardProps {
  action: FactoryActionRequest;
  busy: boolean;
  onStoreCredential: (credentialAlias: string, secretValue: string) => Promise<string | null>;
  onRespond: (response: FactoryActionResponseDraft) => Promise<void>;
}

const statusLabel: Readonly<Record<FactoryActionRequest["status"], string>> = {
  pending: "Pending",
  answered: "Answered",
  denied: "Denied",
  expired: "Expired",
  consumed: "Consumed",
};

function idempotencyKey(): string {
  const values = new Uint8Array(32);
  crypto.getRandomValues(values);
  return Array.from(values, (value) => value.toString(16).padStart(2, "0")).join("");
}

function actionLabel(action: FactoryActionRequest, decision: FactoryActionDecision): string {
  const verb = decision === "approve" ? "Approve" : "Deny";
  if (action.requestType === "n8n_approval") return `${verb} n8n scope`;
  if (action.requestType === "external_api_approval") return `${verb} API scope`;
  if (action.requestType === "credential_required") return decision === "approve" ? "Fortsetzen" : "Deny credential request";
  return decision === "approve" ? "Send clarification" : "Deny clarification";
}

export function FactoryActionCard({ action, busy, onStoreCredential, onRespond }: FactoryActionCardProps) {
  const [answer, setAnswer] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [credentialRef, setCredentialRef] = useState<string | null>(null);
  const canRespond = action.status === "pending";

  async function storeCredential() {
    if (secretValue.length === 0 || busy) return;
    const submittedSecret = secretValue;
    setSecretValue("");
    const reference = await onStoreCredential(action.requiredScope, submittedSecret);
    if (reference !== null) setCredentialRef(reference);
  }

  async function respond(decision: FactoryActionDecision) {
    if (!canRespond || busy || !action.allowedDecisions.includes(decision)) return;
    await onRespond({
      expectedSubjectVersion: action.expectedSubjectVersion,
      decision,
      approvedScope: action.requiredScope,
      credentialRef: action.requestType === "credential_required" && decision === "approve" ? credentialRef : null,
      answerMetadata: action.requestType === "clarification" && decision === "approve" ? { answer } : {},
      answeredAt: new Date().toISOString(),
      idempotencyKey: idempotencyKey(),
    });
  }

  return (
    <article className="factory-action-card" aria-busy={busy}>
      <div className="card-heading">
        <div>
          <p className="eyebrow">Factory action</p>
          <h2>{action.requestType.replaceAll("_", " ")}</h2>
        </div>
        <span className={`status status--${action.status}`} data-status={action.status}>{statusLabel[action.status]}</span>
      </div>
      <p>{action.message}</p>
      <dl className="factory-action-details">
        <div><dt>Required scope</dt><dd>{action.requiredScope}</dd></div>
        <div><dt>Deadline</dt><dd>{action.deadlineAt}</dd></div>
        <div><dt>Last safe checkpoint</dt><dd>{action.safeCheckpointRef.uri}</dd></div>
      </dl>
      {canRespond && action.requestType === "clarification" && (
        <div className="factory-action-form">
          <label htmlFor={`clarification-${action.requestId}`}>Clarification response</label>
          <textarea id={`clarification-${action.requestId}`} value={answer} onChange={(event) => setAnswer(event.target.value)} maxLength={500} />
        </div>
      )}
      {canRespond && action.requestType === "credential_required" && (
        <div className="factory-action-form">
          <label htmlFor={`credential-${action.requestId}`}>Credential value</label>
          <input id={`credential-${action.requestId}`} type="password" autoComplete="off" value={secretValue} onChange={(event) => setSecretValue(event.target.value)} />
          <button disabled={busy || secretValue.length === 0} type="button" onClick={() => void storeCredential()}>Credential verbinden</button>
        </div>
      )}
      {canRespond && (
        <div className="card-actions">
          {action.allowedDecisions.map((decision) => (
            <button
              key={decision}
              disabled={busy || (action.requestType === "credential_required" && decision === "approve" && credentialRef === null) || (action.requestType === "clarification" && decision === "approve" && answer.trim().length === 0)}
              type="button"
              onClick={() => void respond(decision)}
            >
              {actionLabel(action, decision)}
            </button>
          ))}
        </div>
      )}
    </article>
  );
}

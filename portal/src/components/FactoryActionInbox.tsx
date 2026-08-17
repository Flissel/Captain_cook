import { useState } from "react";
import { PortalPublicError, type PortalApi } from "../api";
import type { FactoryActionRequest, FactoryActionResponseDraft } from "../types";
import { FactoryActionCard } from "./FactoryActionCard";

interface FactoryActionInboxProps {
  api: PortalApi;
  actions: FactoryActionRequest[];
  getAccessToken: () => Promise<string>;
  onRefresh: () => Promise<void>;
  onError?: (message: string) => void;
}

export function FactoryActionInbox({ api, actions, getAccessToken, onRefresh, onError }: FactoryActionInboxProps) {
  const [busyRequestId, setBusyRequestId] = useState<string | null>(null);

  async function handleFailure(error: unknown) {
    if (error instanceof PortalPublicError && error.status === 409) {
      await onRefresh();
      return;
    }
    onError?.(error instanceof PortalPublicError ? error.message : "The integration request could not be completed.");
  }

  async function storeCredential(requestId: string, credentialAlias: string, secretValue: string): Promise<string | null> {
    setBusyRequestId(requestId);
    try {
      return await api.storeFactoryCredential(requestId, credentialAlias, secretValue, await getAccessToken());
    } catch (error) {
      await handleFailure(error);
      return null;
    } finally {
      setBusyRequestId(null);
    }
  }

  async function respond(requestId: string, response: FactoryActionResponseDraft): Promise<void> {
    setBusyRequestId(requestId);
    try {
      await api.respondToFactoryAction(requestId, response, await getAccessToken());
      await onRefresh();
    } catch (error) {
      await handleFailure(error);
    } finally {
      setBusyRequestId(null);
    }
  }

  return (
    <section className="factory-action-inbox" aria-labelledby="factory-actions-title">
      <div className="surface-summary">
        <div><p className="eyebrow">Captain action inbox</p><h2 id="factory-actions-title">Factory actions</h2></div>
        <span>{actions.length} action{actions.length === 1 ? "" : "s"}</span>
      </div>
      <div className="factory-action-grid" aria-live="polite">
        {actions.length === 0 ? <p>No factory actions need attention.</p> : actions.map((action) => (
          <FactoryActionCard
            key={action.requestId}
            action={action}
            busy={busyRequestId === action.requestId}
            onStoreCredential={(credentialAlias, secretValue) => storeCredential(action.requestId, credentialAlias, secretValue)}
            onRespond={(response) => respond(action.requestId, response)}
          />
        ))}
      </div>
    </section>
  );
}

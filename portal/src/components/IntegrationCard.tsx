import type { IntegrationSetupAction } from "../types";
import { SetupStatus } from "./SetupStatus";

interface IntegrationCardProps {
  action: IntegrationSetupAction;
  n8nCredentialsUrl: string;
  busy: boolean;
  onDiscover: () => Promise<void> | void;
  onSelect: (credentialId: string) => Promise<void> | void;
  onVerify: () => Promise<void> | void;
  onRotate: () => Promise<void> | void;
  onRevoke: () => Promise<void> | void;
}

function ProjectMetadata({ name, id }: { name: string | null; id: string | null }) {
  if (name === null && id === null) return null;
  return <span className="credential-project">Project: {name ?? id}</span>;
}

export function IntegrationCard({
  action,
  n8nCredentialsUrl,
  busy,
  onDiscover,
  onSelect,
  onVerify,
  onRotate,
  onRevoke,
}: IntegrationCardProps) {
  const canMutateSelection = action.status === "selection_required";
  const canRotateOrRevoke = action.selectedCredential !== null && action.status !== "revoked";
  const canVerify = ["verification_required", "verification_failed", "expired"].includes(action.status);

  return (
    <article className="integration-card" aria-busy={busy}>
      <div className="card-heading">
        <div>
          <p className="eyebrow">Connection setup</p>
          <h2>{action.setupLabel}</h2>
          <p className="credential-type">{action.credentialType}</p>
        </div>
        <SetupStatus status={action.status} />
      </div>

      {action.selectedCredential && (
        <section className="selected-credential" aria-label="Selected connection">
          <span className="metadata-label">Selected</span>
          <strong>{action.selectedCredential.credentialName}</strong>
          <span>{action.selectedCredential.credentialType}</span>
          <ProjectMetadata
            name={action.selectedCredential.projectName}
            id={action.selectedCredential.projectId}
          />
        </section>
      )}

      {canMutateSelection && action.candidateCredentials.length > 0 && (
        <div className="candidate-list" aria-label="Available connections">
          {action.candidateCredentials.map((credential) => (
            <div className="candidate" key={credential.credentialId}>
              <div>
                <strong>{credential.credentialName}</strong>
                <span>{credential.credentialType}</span>
                <ProjectMetadata name={credential.projectName} id={credential.projectId} />
              </div>
              <button disabled={busy} type="button" onClick={() => void onSelect(credential.credentialId)}>
                Use {credential.credentialName}
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="card-actions">
        <a href={n8nCredentialsUrl} target="_blank" rel="noopener noreferrer">
          Connect in n8n
        </a>
        <button disabled={busy} type="button" onClick={() => void onDiscover()}>
          {canMutateSelection ? "Refresh connections" : "Check connections"}
        </button>
        {canVerify && (
          <button disabled={busy} type="button" onClick={() => void onVerify()}>
            Verify connection
          </button>
        )}
        {canRotateOrRevoke && (
          <>
            <button disabled={busy} type="button" onClick={() => void onRotate()}>
              Rotate connection
            </button>
            <button className="danger" disabled={busy} type="button" onClick={() => void onRevoke()}>
              Revoke connection
            </button>
          </>
        )}
      </div>
      {busy && <p role="status" className="busy-message">Updating setup…</p>}
    </article>
  );
}

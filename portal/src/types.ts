export const integrationSetupStatuses = [
  "missing",
  "selection_required",
  "verification_required",
  "verification_failed",
  "ready",
  "revoked",
  "expired",
] as const;

export type IntegrationSetupStatus = (typeof integrationSetupStatuses)[number];

export interface CredentialMetadata {
  credentialId: string;
  credentialName: string;
  credentialType: string;
  projectId: string | null;
  projectName: string | null;
}

export interface IntegrationSetupAction {
  integrationKey: string;
  credentialAlias: string;
  credentialType: string;
  setupLabel: string;
  required: boolean;
  status: IntegrationSetupStatus;
  candidateCredentials: CredentialMetadata[];
  selectedCredential: CredentialMetadata | null;
}

export interface PortalSetupSurface {
  jobId: string;
  revision: number;
  contentSha256: string;
  overallStatus: IntegrationSetupStatus;
  n8nCredentialsUrl: string;
  actions: IntegrationSetupAction[];
}

export interface PortalSetupTicket {
  ticketId: string;
  ticket: string;
  jobId: string;
  credentialAlias: string;
  expiresAt: string;
}

export const factoryActionRequestTypes = [
  "clarification",
  "credential_required",
  "external_api_approval",
  "n8n_approval",
] as const;

export const factoryActionStatuses = ["pending", "answered", "denied", "expired", "consumed"] as const;

export const factoryActionDecisions = ["approve", "deny"] as const;

export type FactoryActionRequestType = (typeof factoryActionRequestTypes)[number];
export type FactoryActionStatus = (typeof factoryActionStatuses)[number];
export type FactoryActionDecision = (typeof factoryActionDecisions)[number];

export interface FactoryArtifactReference {
  uri: string;
  sha256: string;
  mediaType: "application/json";
}

export interface FactoryActionRequest {
  requestId: string;
  factoryJobId: string;
  creationJobId: string;
  correlationId: string;
  expectedSubjectVersion: number;
  requestType: FactoryActionRequestType;
  safeCheckpointRef: FactoryArtifactReference;
  safeCheckpointSha256: string;
  requiredScope: string;
  allowedDecisions: FactoryActionDecision[];
  message: string;
  deadlineAt: string;
  status: FactoryActionStatus;
}

export interface FactoryActionResponseDraft {
  expectedSubjectVersion: number;
  decision: FactoryActionDecision;
  approvedScope: string;
  credentialRef: string | null;
  answerMetadata: Readonly<Record<string, string>>;
  answeredAt: string;
  idempotencyKey: string;
}

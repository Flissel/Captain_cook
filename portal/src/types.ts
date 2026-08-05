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

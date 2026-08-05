import type { IntegrationSetupAction, PortalSetupSurface } from "../types";

export const missingAction: IntegrationSetupAction = {
  integrationKey: "crm",
  credentialAlias: "CRM_PRIMARY",
  credentialType: "hubspotApi",
  setupLabel: "Connect the CRM",
  required: true,
  status: "missing",
  candidateCredentials: [],
  selectedCredential: null,
};

export const surface: PortalSetupSurface = {
  jobId: "10000000-0000-0000-0000-000000000001",
  revision: 1,
  contentSha256: "a".repeat(64),
  overallStatus: "missing",
  n8nCredentialsUrl: "https://n8n.example.test/home/credentials",
  actions: [missingAction],
};

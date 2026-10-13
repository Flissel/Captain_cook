import type { IntegrationSetupAction, PortalSetupSurface } from "../types";
import type { FactoryActionRequest } from "../types";

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

export const credentialAction: FactoryActionRequest = {
  requestId: "00000000-0000-0000-0000-000000000601",
  factoryJobId: "00000000-0000-0000-0000-000000000301",
  creationJobId: "00000000-0000-0000-0000-000000000401",
  correlationId: "00000000-0000-0000-0000-000000000302",
  expectedSubjectVersion: 1,
  requestType: "credential_required",
  safeCheckpointRef: {
    uri: "artifact://minibook/checkpoint/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    sha256: "a".repeat(64),
    mediaType: "application/json",
  },
  safeCheckpointSha256: "a".repeat(64),
  requiredScope: "n8n.crm.read",
  allowedDecisions: ["approve", "deny"],
  message: "A CRM credential is required.",
  deadlineAt: "2026-08-09T12:00:00Z",
  status: "pending",
};

export const clarificationAction: FactoryActionRequest = {
  ...credentialAction,
  requestId: "00000000-0000-0000-0000-000000000602",
  requestType: "clarification",
  message: "Which sales region should the team prioritize?",
};

export const n8nApprovalAction: FactoryActionRequest = {
  ...credentialAction,
  requestId: "00000000-0000-0000-0000-000000000603",
  requestType: "n8n_approval",
  requiredScope: "n8n.salesforce.write",
  message: "Approve the Salesforce write scope.",
};

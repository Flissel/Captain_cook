import {
  integrationSetupStatuses,
  type CredentialMetadata,
  factoryActionDecisions,
  factoryActionRequestTypes,
  factoryActionStatuses,
  type FactoryActionDecision,
  type FactoryActionRequest,
  type FactoryActionRequestType,
  type FactoryActionResponseDraft,
  type FactoryActionStatus,
  type IntegrationSetupAction,
  type IntegrationSetupStatus,
  type PortalSetupSurface,
  type PortalSetupTicket,
} from "./types";

type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;
type TicketAction = "discover" | "select" | "verify" | "rotation_requested" | "revoked";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
const CREDENTIAL_ALIAS_PATTERN = /^[A-Z][A-Z0-9_]*$/;
const NON_WHITESPACE_PATTERN = /^\S+$/;
const CREDENTIAL_TYPE_PATTERN = /^[A-Za-z][A-Za-z0-9_]{0,127}$/;
const PORTAL_API_PREFIX = "/v1/portal";
const OPAQUE_TOKEN_PATTERN = /[A-Za-z0-9_+/-]{32,}={0,2}/g;
const OPAQUE_TOKEN_SHAPE = /^[A-Za-z0-9_+/-]{32,}={0,2}$/;
const HUMAN_READABLE_IDENTIFIER_PATTERN = /^[a-z]+(?:[_-](?:[a-z]+|v\d{1,3}|\d{1,8}))+$/;
const UUID_VALUE_PATTERN = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
const KNOWN_SECRET_PATTERN = /\b(?:sk_(?:live|test)_[A-Za-z0-9_-]+|sk-proj-[A-Za-z0-9_-]+|gh[pours]_[A-Za-z0-9_-]+|xox[a-z]-[A-Za-z0-9-]+|AIza[A-Za-z0-9_-]+|(?:AKIA|ASIA)[A-Z0-9]{16}|[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,})/i;

export class PortalPublicError extends Error {
  readonly status!: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = "PortalPublicError";
    Object.defineProperty(this, "status", { value: status, enumerable: false });
  }
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("expected object");
  }
  return value as Record<string, unknown>;
}

function exact(value: unknown, keys: readonly string[]): Record<string, unknown> {
  const candidate = record(value);
  const actual = Object.keys(candidate).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new Error("unexpected response shape");
  }
  return candidate;
}

function stringValue(value: unknown, pattern?: RegExp, maximumLength?: number): string {
  if (
    typeof value !== "string" ||
    [...value].length === 0 ||
    (maximumLength !== undefined && [...value].length > maximumLength) ||
    (pattern && !pattern.test(value))
  ) {
    throw new Error("invalid string");
  }
  return value;
}

function nullableString(value: unknown, pattern?: RegExp, maximumLength?: number): string | null {
  return value === null ? null : stringValue(value, pattern, maximumLength);
}

function statusValue(value: unknown): IntegrationSetupStatus {
  if (typeof value !== "string" || !integrationSetupStatuses.includes(value as IntegrationSetupStatus)) {
    throw new Error("invalid status");
  }
  return value as IntegrationSetupStatus;
}

function factoryActionStatusValue(value: unknown): FactoryActionStatus {
  if (typeof value !== "string" || !factoryActionStatuses.includes(value as FactoryActionStatus)) {
    throw new Error("invalid factory action status");
  }
  return value as FactoryActionStatus;
}

function factoryActionRequestTypeValue(value: unknown): FactoryActionRequestType {
  if (typeof value !== "string" || !factoryActionRequestTypes.includes(value as FactoryActionRequestType)) {
    throw new Error("invalid factory action request type");
  }
  return value as FactoryActionRequestType;
}

function factoryActionDecisionValue(value: unknown): FactoryActionDecision {
  if (typeof value !== "string" || !factoryActionDecisions.includes(value as FactoryActionDecision)) {
    throw new Error("invalid factory action decision");
  }
  return value as FactoryActionDecision;
}

function safeHttpUrl(value: unknown): string {
  const raw = stringValue(value);
  const parsed = new URL(raw);
  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    throw new Error("unsafe URL");
  }
  return raw;
}

function utcTimestamp(value: unknown): string {
  const timestamp = stringValue(value, undefined, 64);
  if (!timestamp.endsWith("Z") || Number.isNaN(Date.parse(timestamp))) {
    throw new Error("invalid UTC timestamp");
  }
  return timestamp;
}

function safeFactoryText(value: unknown, maximumLength: number): string {
  const text = stringValue(value, undefined, maximumLength);
  if (/\b(?:bearer|basic)\s+|(?:api[_ -]?key|password|secret|token|credential)\s*=|credential:\/\/|private.?key|system.?prompt|provider.?response|holdout/i.test(text) || knownOrOpaqueSecret(text, false)) {
    throw new Error("unsafe factory display text");
  }
  return text;
}

function tokenEntropy(token: string): number {
  return -Array.from(new Set(token), (character) => {
    const probability = token.split(character).length - 1;
    const ratio = probability / token.length;
    return ratio * Math.log2(ratio);
  }).reduce((total, value) => total + value, 0);
}

function humanReadableIdentifier(token: string): boolean {
  return /^[a-z]+$/.test(token) || /^[A-Z][a-z]+$/.test(token) || HUMAN_READABLE_IDENTIFIER_PATTERN.test(token);
}

function knownOrOpaqueSecret(value: string, allowDigest: boolean, uriSegments = false): boolean {
  if (KNOWN_SECRET_PATTERN.test(value)) return true;
  const tokens = uriSegments
    ? value.replace("artifact://", "").split("/")
    : value.match(OPAQUE_TOKEN_PATTERN) ?? [];
  for (const token of tokens) {
    if (UUID_VALUE_PATTERN.test(token)) continue;
    if (/^[0-9a-f]{64}$/i.test(token)) {
      if (!allowDigest) return true;
      continue;
    }
    if (!OPAQUE_TOKEN_SHAPE.test(token) || humanReadableIdentifier(token)) continue;
    const hasOpaqueAlphabet = /[0-9_+/-=]/.test(token) || (/[a-z]/.test(token) && /[A-Z]/.test(token));
    if (/^[0-9a-f]{32,}$/i.test(token) || (hasOpaqueAlphabet && tokenEntropy(token) >= 3.5)) return true;
  }
  return false;
}

function parseFactoryArtifactReference(value: unknown) {
  const item = exact(value, ["uri", "sha256", "media_type"]);
  const uri = stringValue(item.uri, /^artifact:\/\/[A-Za-z0-9._:/-]+$/, 512);
  if (knownOrOpaqueSecret(uri, true, true)) throw new Error("unsafe checkpoint reference");
  const sha256 = stringValue(item.sha256, SHA256_PATTERN);
  if (item.media_type !== "application/json") throw new Error("invalid checkpoint media type");
  return { uri, sha256, mediaType: "application/json" as const };
}

export function parseFactoryAction(value: unknown): FactoryActionRequest {
  const item = exact(value, [
    "schema", "request_id", "factory_job_id", "creation_job_id", "correlation_id",
    "expected_subject_version", "request_type", "safe_checkpoint_ref", "safe_checkpoint_sha256",
    "required_scope", "allowed_decisions", "message", "deadline_at", "status",
  ]);
  if (item.schema !== "captain.factory-action-request.v1" || !Number.isInteger(item.expected_subject_version) || Number(item.expected_subject_version) < 1 || !Array.isArray(item.allowed_decisions)) {
    throw new Error("invalid factory action");
  }
  const allowedDecisions = item.allowed_decisions.map(factoryActionDecisionValue);
  if (allowedDecisions.length === 0 || new Set(allowedDecisions).size !== allowedDecisions.length) {
    throw new Error("invalid factory action decisions");
  }
  const safeCheckpointRef = parseFactoryArtifactReference(item.safe_checkpoint_ref);
  const safeCheckpointSha256 = stringValue(item.safe_checkpoint_sha256, SHA256_PATTERN);
  if (safeCheckpointSha256 !== safeCheckpointRef.sha256) throw new Error("checkpoint mismatch");
  return {
    requestId: stringValue(item.request_id, UUID_PATTERN),
    factoryJobId: stringValue(item.factory_job_id, UUID_PATTERN),
    creationJobId: stringValue(item.creation_job_id, UUID_PATTERN),
    correlationId: stringValue(item.correlation_id, UUID_PATTERN),
    expectedSubjectVersion: Number(item.expected_subject_version),
    requestType: factoryActionRequestTypeValue(item.request_type),
    safeCheckpointRef,
    safeCheckpointSha256,
    requiredScope: safeFactoryText(item.required_scope, 200),
    allowedDecisions,
    message: safeFactoryText(item.message, 500),
    deadlineAt: utcTimestamp(item.deadline_at),
    status: factoryActionStatusValue(item.status),
  };
}

function parseFactoryCredentialReference(value: unknown): string {
  const item = exact(value, ["credential_ref"]);
  return stringValue(item.credential_ref, /^credential:\/\/[A-Za-z0-9._/-]+$/, 512);
}

function parseFactoryActionResponse(value: unknown, requestId: string): void {
  const item = exact(value, [
    "schema", "request_id", "expected_subject_version", "decision", "approved_scope",
    "credential_ref", "answer_metadata", "answered_at", "idempotency_key",
  ]);
  if (
    item.schema !== "captain.factory-action-response.v1" ||
    stringValue(item.request_id, UUID_PATTERN) !== requestId ||
    !Number.isInteger(item.expected_subject_version) ||
    Number(item.expected_subject_version) < 1 ||
    item.credential_ref !== null && !/^credential:\/\/[A-Za-z0-9._/-]+$/.test(stringValue(item.credential_ref, undefined, 512))
  ) {
    throw new Error("invalid factory action response");
  }
  factoryActionDecisionValue(item.decision);
  safeFactoryText(item.approved_scope, 200);
  record(item.answer_metadata);
  utcTimestamp(item.answered_at);
  stringValue(item.idempotency_key, SHA256_PATTERN);
}

function parseCredential(value: unknown): CredentialMetadata {
  const item = exact(value, [
    "schema",
    "credential_id",
    "credential_name",
    "credential_type",
    "project_id",
    "project_name",
  ]);
  if (item.schema !== "captain.n8n-credential-metadata.v1") {
    throw new Error("invalid credential schema");
  }
  return {
    credentialId: stringValue(item.credential_id, NON_WHITESPACE_PATTERN, 256),
    credentialName: stringValue(item.credential_name, undefined, 256),
    credentialType: stringValue(item.credential_type, CREDENTIAL_TYPE_PATTERN),
    projectId: nullableString(item.project_id, NON_WHITESPACE_PATTERN, 256),
    projectName: nullableString(item.project_name, undefined, 256),
  };
}

function parseAction(value: unknown): IntegrationSetupAction {
  const item = exact(value, [
    "integration_key",
    "credential_alias",
    "credential_type",
    "setup_label",
    "required",
    "status",
    "candidate_credentials",
    "selected_credential",
  ]);
  if (!Array.isArray(item.candidate_credentials) || typeof item.required !== "boolean") {
    throw new Error("invalid integration action");
  }
  return {
    integrationKey: stringValue(item.integration_key, IDENTIFIER_PATTERN, 128),
    credentialAlias: stringValue(item.credential_alias, CREDENTIAL_ALIAS_PATTERN, 128),
    credentialType: stringValue(item.credential_type, CREDENTIAL_TYPE_PATTERN),
    setupLabel: stringValue(item.setup_label, undefined, 128),
    required: item.required,
    status: statusValue(item.status),
    candidateCredentials: item.candidate_credentials.map(parseCredential),
    selectedCredential: item.selected_credential === null ? null : parseCredential(item.selected_credential),
  };
}

export function parseSetupSurface(value: unknown): PortalSetupSurface {
  const item = exact(value, [
    "job_id",
    "revision",
    "content_sha256",
    "overall_status",
    "n8n_credentials_url",
    "actions",
  ]);
  if (!Number.isInteger(item.revision) || Number(item.revision) < 1 || !Array.isArray(item.actions)) {
    throw new Error("invalid setup surface");
  }
  return {
    jobId: stringValue(item.job_id, UUID_PATTERN),
    revision: Number(item.revision),
    contentSha256: stringValue(item.content_sha256, SHA256_PATTERN),
    overallStatus: statusValue(item.overall_status),
    n8nCredentialsUrl: safeHttpUrl(item.n8n_credentials_url),
    actions: item.actions.map(parseAction),
  };
}

function parseTicket(value: unknown): PortalSetupTicket {
  const item = exact(value, ["ticket_id", "ticket", "job_id", "credential_alias", "expires_at"]);
  const expiresAt = stringValue(item.expires_at);
  if (Number.isNaN(Date.parse(expiresAt))) {
    throw new Error("invalid expiry");
  }
  return {
    ticketId: stringValue(item.ticket_id, UUID_PATTERN),
    ticket: stringValue(item.ticket, undefined, 256),
    jobId: stringValue(item.job_id, UUID_PATTERN),
    credentialAlias: stringValue(item.credential_alias, IDENTIFIER_PATTERN, 128),
    expiresAt,
  };
}

function publicStatusError(status: number): PortalPublicError {
  const messages: Readonly<Record<number, string>> = {
    401: "Your session has expired. Sign in again.",
    403: "You do not have access to this setup.",
    404: "This integration setup was not found.",
    409: "The setup changed. Refresh and try again.",
    503: "The integration service is temporarily unavailable.",
  };
  return new PortalPublicError(messages[status] ?? "The integration request could not be completed.", status);
}

async function rawRequest(
  fetcher: Fetcher,
  url: string,
  accessToken: string,
  init: RequestInit,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetcher(url, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
        Authorization: `Bearer ${accessToken}`,
      },
    });
  } catch {
    throw new PortalPublicError("The integration service could not be reached.");
  }
  if (!response.ok) {
    throw publicStatusError(response.status);
  }
  try {
    return await response.json();
  } catch {
    throw new PortalPublicError("The integration service returned an invalid response.");
  }
}

export interface PortalApi {
  getSetupSurface(jobId: string, accessToken: string): Promise<PortalSetupSurface>;
  discoverCredentials(jobId: string, alias: string, accessToken: string): Promise<PortalSetupSurface>;
  selectCredential(jobId: string, alias: string, credentialId: string, accessToken: string): Promise<PortalSetupSurface>;
  verifyCredential(jobId: string, alias: string, accessToken: string): Promise<PortalSetupSurface>;
  requestRotation(jobId: string, alias: string, accessToken: string): Promise<PortalSetupSurface>;
  revokeCredential(jobId: string, alias: string, accessToken: string): Promise<PortalSetupSurface>;
  getFactoryActions(accessToken: string, status?: FactoryActionStatus): Promise<FactoryActionRequest[]>;
  storeFactoryCredential(requestId: string, credentialAlias: string, secretValue: string, accessToken: string): Promise<string>;
  respondToFactoryAction(requestId: string, response: FactoryActionResponseDraft, accessToken: string): Promise<void>;
}

export function createPortalApi(fetcher: Fetcher = fetch): PortalApi {
  const setupPath = (jobId: string) =>
    `${PORTAL_API_PREFIX}/integration-setups/${encodeURIComponent(jobId)}`;
  const factoryActionPath = (requestId: string) =>
    `${PORTAL_API_PREFIX}/factory-actions/${encodeURIComponent(requestId)}`;

  const surfaceRequest = async (
    url: string,
    accessToken: string,
    init: RequestInit = { method: "GET" },
  ): Promise<PortalSetupSurface> => {
    const value = await rawRequest(fetcher, url, accessToken, init);
    try {
      return parseSetupSurface(value);
    } catch {
      throw new PortalPublicError("The integration service returned an invalid response.");
    }
  };

  const issueTicket = async (
    jobId: string,
    alias: string,
    action: TicketAction,
    accessToken: string,
  ): Promise<PortalSetupTicket> => {
    const value = await rawRequest(fetcher, `${setupPath(jobId)}/tickets`, accessToken, {
      method: "POST",
      body: JSON.stringify({ credential_alias: alias, action }),
    });
    try {
      const ticket = parseTicket(value);
      if (ticket.jobId !== jobId || ticket.credentialAlias !== alias) {
        throw new Error("ticket binding mismatch");
      }
      return ticket;
    } catch {
      throw new PortalPublicError("The integration service returned an invalid response.");
    }
  };

  const consume = async (
    jobId: string,
    alias: string,
    action: TicketAction,
    route: "discover" | "select" | "verify" | "actions",
    accessToken: string,
    additional: Readonly<Record<string, string>> = {},
  ): Promise<PortalSetupSurface> => {
    const ticket = await issueTicket(jobId, alias, action, accessToken);
    return surfaceRequest(`${setupPath(jobId)}/${route}`, accessToken, {
      method: "POST",
      body: JSON.stringify({
        ticket_id: ticket.ticketId,
        ticket: ticket.ticket,
        credential_alias: alias,
        ...additional,
      }),
    });
  };

  return {
    getSetupSurface: (jobId, accessToken) => surfaceRequest(setupPath(jobId), accessToken),
    discoverCredentials: (jobId, alias, accessToken) =>
      consume(jobId, alias, "discover", "discover", accessToken),
    selectCredential: (jobId, alias, credentialId, accessToken) =>
      consume(jobId, alias, "select", "select", accessToken, { credential_id: credentialId }),
    verifyCredential: (jobId, alias, accessToken) =>
      consume(jobId, alias, "verify", "verify", accessToken),
    requestRotation: (jobId, alias, accessToken) =>
      consume(jobId, alias, "rotation_requested", "actions", accessToken, {
        action: "rotation_requested",
      }),
    revokeCredential: (jobId, alias, accessToken) =>
      consume(jobId, alias, "revoked", "actions", accessToken, { action: "revoked" }),
    getFactoryActions: async (accessToken, status = "pending") => {
      const value = await rawRequest(fetcher, `${PORTAL_API_PREFIX}/factory-actions?status=${encodeURIComponent(status)}`, accessToken, { method: "GET" });
      try {
        if (!Array.isArray(value)) throw new Error("factory action list must be an array");
        return value.map(parseFactoryAction);
      } catch {
        throw new PortalPublicError("The integration service returned an invalid response.");
      }
    },
    storeFactoryCredential: async (requestId, credentialAlias, secretValue, accessToken) => {
      const value = await rawRequest(fetcher, `${factoryActionPath(requestId)}/credentials`, accessToken, {
        method: "POST",
        body: JSON.stringify({ credential_alias: credentialAlias, secret_value: secretValue }),
      });
      try {
        return parseFactoryCredentialReference(value);
      } catch {
        throw new PortalPublicError("The integration service returned an invalid response.");
      }
    },
    respondToFactoryAction: async (requestId, response, accessToken) => {
      const value = await rawRequest(fetcher, `${factoryActionPath(requestId)}/responses`, accessToken, {
        method: "POST",
        body: JSON.stringify({
          schema: "captain.factory-action-response.v1",
          request_id: requestId,
          expected_subject_version: response.expectedSubjectVersion,
          decision: response.decision,
          approved_scope: response.approvedScope,
          credential_ref: response.credentialRef,
          answer_metadata: response.answerMetadata,
          answered_at: response.answeredAt,
          idempotency_key: response.idempotencyKey,
        }),
      });
      try {
        parseFactoryActionResponse(value, requestId);
      } catch {
        throw new PortalPublicError("The integration service returned an invalid response.");
      }
    },
  };
}

export function getSetupSurface(jobId: string, accessToken: string): Promise<PortalSetupSurface> {
  return createPortalApi().getSetupSurface(jobId, accessToken);
}

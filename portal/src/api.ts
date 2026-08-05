import {
  integrationSetupStatuses,
  type CredentialMetadata,
  type IntegrationSetupAction,
  type IntegrationSetupStatus,
  type PortalSetupSurface,
  type PortalSetupTicket,
} from "./types";

type Fetcher = (input: string | URL | Request, init?: RequestInit) => Promise<Response>;
type TicketAction = "discover" | "select" | "rotation_requested" | "revoked";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
const CREDENTIAL_ALIAS_PATTERN = /^[A-Z][A-Z0-9_]*$/;
const NON_WHITESPACE_PATTERN = /^\S+$/;
const CREDENTIAL_TYPE_PATTERN = /^[A-Za-z][A-Za-z0-9_]{0,127}$/;
const PORTAL_API_PREFIX = "/v1/portal";

export class PortalPublicError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PortalPublicError";
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
  return new PortalPublicError(messages[status] ?? "The integration request could not be completed.");
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
  requestRotation(jobId: string, alias: string, accessToken: string): Promise<PortalSetupSurface>;
  revokeCredential(jobId: string, alias: string, accessToken: string): Promise<PortalSetupSurface>;
}

export function createPortalApi(fetcher: Fetcher = fetch): PortalApi {
  const setupPath = (jobId: string) =>
    `${PORTAL_API_PREFIX}/integration-setups/${encodeURIComponent(jobId)}`;

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
    route: "discover" | "select" | "actions",
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
    requestRotation: (jobId, alias, accessToken) =>
      consume(jobId, alias, "rotation_requested", "actions", accessToken, {
        action: "rotation_requested",
      }),
    revokeCredential: (jobId, alias, accessToken) =>
      consume(jobId, alias, "revoked", "actions", accessToken, { action: "revoked" }),
  };
}

export function getSetupSurface(jobId: string, accessToken: string): Promise<PortalSetupSurface> {
  return createPortalApi().getSetupSurface(jobId, accessToken);
}

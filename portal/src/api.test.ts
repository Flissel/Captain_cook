import { describe, expect, it, vi } from "vitest";
import {
  PortalPublicError,
  createPortalApi,
  parseFactoryAction,
  parseSetupSurface,
} from "./api";

const jobId = "10000000-0000-0000-0000-000000000001";
const accessToken = "private-user-jwt";

const surfacePayload = {
  job_id: jobId,
  revision: 1,
  content_sha256: "a".repeat(64),
  overall_status: "selection_required",
  n8n_credentials_url: "https://n8n.example.test/home/credentials",
  actions: [
    {
      integration_key: "crm",
      credential_alias: "CRM_PRIMARY",
      credential_type: "hubspotApi",
      setup_label: "Connect CRM",
      required: true,
      status: "selection_required",
      candidate_credentials: [
        {
          schema: "captain.n8n-credential-metadata.v1",
          credential_id: "credential-1",
          credential_name: "CRM Primary",
          credential_type: "hubspotApi",
          project_id: "project-1",
          project_name: "Sales",
        },
      ],
      selected_credential: null,
    },
  ],
};

const factoryActionPayload = {
  schema: "captain.factory-action-request.v1",
  request_id: "00000000-0000-0000-0000-000000000601",
  factory_job_id: "00000000-0000-0000-0000-000000000301",
  creation_job_id: "00000000-0000-0000-0000-000000000401",
  correlation_id: "00000000-0000-0000-0000-000000000302",
  expected_subject_version: 1,
  request_type: "credential_required",
  safe_checkpoint_ref: {
    uri: "artifact://minibook/checkpoint/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    sha256: "a".repeat(64),
    media_type: "application/json",
  },
  safe_checkpoint_sha256: "a".repeat(64),
  required_scope: "n8n.crm.read",
  allowed_decisions: ["approve", "deny"],
  message: "A CRM credential is required.",
  deadline_at: "2026-08-09T12:00:00Z",
  status: "pending",
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("portal API", () => {
  it("loads strict factory actions and posts credential references without the raw value", async () => {
    const fetcher = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      void init;
      const url = String(input);
      if (url.includes("?status=pending")) return response([factoryActionPayload]);
      if (url.endsWith("/credentials")) return response({ credential_ref: "credential://tenant-a/n8n-salesforce" }, 201);
      if (url.endsWith("/responses")) return response({
        schema: "captain.factory-action-response.v1",
        request_id: factoryActionPayload.request_id,
        expected_subject_version: 1,
        decision: "approve",
        approved_scope: "n8n.crm.read",
        credential_ref: "credential://tenant-a/n8n-salesforce",
        answer_metadata: {},
        answered_at: "2026-08-08T12:00:00.000Z",
        idempotency_key: "c".repeat(64),
      });
      throw new Error(`unexpected URL ${url}`);
    });
    const api = createPortalApi(fetcher);

    await expect(api.getFactoryActions(accessToken)).resolves.toMatchObject([{ requestId: factoryActionPayload.request_id }]);
    await expect(api.storeFactoryCredential(factoryActionPayload.request_id, "n8n.crm.read", "secret-value", accessToken)).resolves.toBe("credential://tenant-a/n8n-salesforce");
    await api.respondToFactoryAction(factoryActionPayload.request_id, {
      expectedSubjectVersion: 1,
      decision: "approve",
      approvedScope: "n8n.crm.read",
      credentialRef: "credential://tenant-a/n8n-salesforce",
      answerMetadata: {},
      answeredAt: "2026-08-08T12:00:00.000Z",
      idempotencyKey: "c".repeat(64),
    }, accessToken);

    const responseRequest = fetcher.mock.calls.find(([url]) => String(url).endsWith("/responses"));
    expect(responseRequest).toBeDefined();
    expect(String(responseRequest?.[1]?.body)).not.toContain("secret-value");
    expect(JSON.parse(String(responseRequest?.[1]?.body))).toMatchObject({
      credential_ref: "credential://tenant-a/n8n-salesforce",
      approved_scope: "n8n.crm.read",
    });
  });

  it("rejects unsafe or malformed factory action payloads", () => {
    expect(() => parseFactoryAction({ ...factoryActionPayload, message: "token=private-value" })).toThrow();
    expect(() => parseFactoryAction({ ...factoryActionPayload, status: "not-a-status" })).toThrow();
    expect(() => parseFactoryAction({ ...factoryActionPayload, safe_checkpoint_sha256: "b".repeat(64) })).toThrow();
  });

  it("fails closed on known and high-entropy tokens in every rendered factory field", () => {
    const longProse = "A display-safe explanation with ordinary words and spaces. ".repeat(5);
    expect(parseFactoryAction({ ...factoryActionPayload, message: longProse }).message).toBe(longProse);
    const germanProse = "Bitte pruefe die Donaudampfschifffahrtsgesellschaft im DACH Markt.";
    expect(parseFactoryAction({ ...factoryActionPayload, message: germanProse }).message).toBe(germanProse);
    const safeIdentifier = "sales_pipeline_brief_for_dach_market_2026";
    expect(parseFactoryAction({ ...factoryActionPayload, required_scope: safeIdentifier }).requiredScope).toBe(safeIdentifier);
    const safeUuid = "123e4567-e89b-12d3-a456-426614174000";
    expect(parseFactoryAction({ ...factoryActionPayload, message: safeUuid }).message).toBe(safeUuid);
    const digest = "0123456789abcdef".repeat(4);
    expect(parseFactoryAction({
      ...factoryActionPayload,
      safe_checkpoint_ref: { ...factoryActionPayload.safe_checkpoint_ref, uri: `artifact://minibook/checkpoint/${digest}`, sha256: digest },
      safe_checkpoint_sha256: digest,
    }).safeCheckpointSha256).toBe(digest);
    for (const patch of [
      { message: "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk" },
      { message: "V2hhdGV2ZXJTdXBlclNlY3JldENyZWRlbnRpYWw" },
      { message: "Q29uZmlkZW50aWFsVG9rZW5Gb3JUZXN0aW5nLTEyMw" },
      { message: "GEekimnOsQapQYnwxawDrwCNtDdVqCxsEcQvpJJTuveD" },
      { message: "aBcDeFgHiJkLmNoPqRsTuVwXyZaBcDeFgHiJkLmNoPqRsTuVwXyZ" },
      { message: "a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef0123456789abcdef" },
      { required_scope: "ghp_abcdefghijklmnopqrstuvwxyz0123456789" },
      { message: "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789" },
      { message: "credential://tenant-a/private-credential" },
      { safe_checkpoint_ref: { ...factoryActionPayload.safe_checkpoint_ref, uri: "artifact://minibook/checkpoint/Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk" } },
    ]) {
      expect(() => parseFactoryAction({ ...factoryActionPayload, ...patch })).toThrow();
    }
  });

  it("uses the bearer header without reflecting the credential in errors", async () => {
    const fetcher = vi.fn(async () => response({ detail: accessToken }, 401));
    const api = createPortalApi(fetcher);

    await expect(api.getSetupSurface(jobId, accessToken)).rejects.toEqual(
      new PortalPublicError("Your session has expired. Sign in again."),
    );
    expect(fetcher).toHaveBeenCalledWith(
      `/v1/portal/integration-setups/${jobId}`,
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: `Bearer ${accessToken}` }),
      }),
    );
    expect(String(fetcher.mock.results[0]?.value)).not.toContain(accessToken);
  });

  it.each([
    [403, "You do not have access to this setup."],
    [404, "This integration setup was not found."],
    [409, "The setup changed. Refresh and try again."],
    [503, "The integration service is temporarily unavailable."],
    [500, "The integration request could not be completed."],
  ])("maps %i to fixed public text", async (status, message) => {
    const api = createPortalApi(async () =>
      response({ detail: "provider response must stay hidden" }, status),
    );
    await expect(api.getSetupSurface(jobId, accessToken)).rejects.toEqual(
      new PortalPublicError(message),
    );
  });

  it("maps network and response parse failures to fixed public text", async () => {
    const networkApi = createPortalApi(async () => {
      throw new Error("private endpoint detail");
    });
    const parseApi = createPortalApi(async () =>
      response({ ...surfacePayload, access_token: "must-not-enter" }),
    );

    await expect(networkApi.getSetupSurface(jobId, accessToken)).rejects.toEqual(
      new PortalPublicError("The integration service could not be reached."),
    );
    await expect(parseApi.getSetupSurface(jobId, accessToken)).rejects.toEqual(
      new PortalPublicError("The integration service returned an invalid response."),
    );
  });

  it("strictly validates nested surface data and safe n8n URLs", () => {
    expect(() => parseSetupSurface({ ...surfacePayload, unexpected: true })).toThrow();
    expect(() =>
      parseSetupSurface({
        ...surfacePayload,
        actions: [{ ...surfacePayload.actions[0], password: "hidden" }],
      }),
    ).toThrow();
    expect(() =>
      parseSetupSurface({
        ...surfacePayload,
        n8n_credentials_url: "javascript:alert(1)",
      }),
    ).toThrow();
    expect(() =>
      parseSetupSurface({
        ...surfacePayload,
        n8n_credentials_url: "https://n8n.example.test/home/credentials?redirect=elsewhere",
      }),
    ).toThrow();
  });

  it.each([
    ["integration key length", { integration_key: "a".repeat(129) }],
    ["credential alias pattern", { credential_alias: "crm_primary" }],
    ["credential alias length", { credential_alias: `A${"B".repeat(128)}` }],
    ["setup label length", { setup_label: "L".repeat(129) }],
  ])("rejects backend-incompatible %s", (_name, actionPatch) => {
    expect(() =>
      parseSetupSurface({
        ...surfacePayload,
        actions: [{ ...surfacePayload.actions[0], ...actionPatch }],
      }),
    ).toThrow();
  });

  it.each([
    ["credential id whitespace", { credential_id: "credential id" }],
    ["credential id length", { credential_id: "c".repeat(257) }],
    ["credential name length", { credential_name: "N".repeat(257) }],
    ["project id whitespace", { project_id: "project id" }],
    ["project id length", { project_id: "p".repeat(257) }],
    ["project name length", { project_name: "P".repeat(257) }],
  ])("rejects backend-incompatible %s", (_name, credentialPatch) => {
    expect(() =>
      parseSetupSurface({
        ...surfacePayload,
        actions: [
          {
            ...surfacePayload.actions[0],
            candidate_credentials: [
              { ...surfacePayload.actions[0].candidate_credentials[0], ...credentialPatch },
            ],
          },
        ],
      }),
    ).toThrow();
  });

  it("rejects an oversized setup ticket before sending a consume request", async () => {
    const fetcher = vi.fn(async () =>
      response(
        {
          ticket_id: "20000000-0000-0000-0000-000000000001",
          ticket: "t".repeat(257),
          job_id: jobId,
          credential_alias: "CRM_PRIMARY",
          expires_at: "2026-08-05T12:10:00Z",
        },
        201,
      ),
    );
    const api = createPortalApi(fetcher);

    await expect(api.discoverCredentials(jobId, "CRM_PRIMARY", accessToken)).rejects.toEqual(
      new PortalPublicError("The integration service returned an invalid response."),
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("issues and consumes a fresh ticket for every exact operation", async () => {
    let ticketNumber = 0;
    const fetcher = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/tickets")) {
        ticketNumber += 1;
        return response(
          {
            ticket_id: `20000000-0000-0000-0000-00000000000${ticketNumber}`,
            ticket: `opaque-${ticketNumber}`,
            job_id: jobId,
            credential_alias: "CRM_PRIMARY",
            expires_at: "2026-08-05T12:10:00Z",
          },
          201,
        );
      }
      expect(init?.body).toBeTypeOf("string");
      return response(surfacePayload);
    });
    const api = createPortalApi(fetcher);

    await api.discoverCredentials(jobId, "CRM_PRIMARY", accessToken);
    await api.selectCredential(jobId, "CRM_PRIMARY", "credential-1", accessToken);
    await api.verifyCredential(jobId, "CRM_PRIMARY", accessToken);
    await api.requestRotation(jobId, "CRM_PRIMARY", accessToken);
    await api.revokeCredential(jobId, "CRM_PRIMARY", accessToken);

    const calls = fetcher.mock.calls;
    expect(calls.filter(([url]) => String(url).endsWith("/tickets"))).toHaveLength(5);
    expect(JSON.parse(String(calls[1]?.[1]?.body))).toEqual({
      ticket_id: "20000000-0000-0000-0000-000000000001",
      ticket: "opaque-1",
      credential_alias: "CRM_PRIMARY",
    });
    expect(JSON.parse(String(calls[3]?.[1]?.body))).toEqual({
      ticket_id: "20000000-0000-0000-0000-000000000002",
      ticket: "opaque-2",
      credential_alias: "CRM_PRIMARY",
      credential_id: "credential-1",
    });
    expect(JSON.parse(String(calls[5]?.[1]?.body))).toMatchObject({ ticket: "opaque-3" });
    expect(String(calls[5]?.[0])).toContain("/verify");
    expect(JSON.parse(String(calls[7]?.[1]?.body))).toMatchObject({
      ticket: "opaque-4",
      action: "rotation_requested",
    });
    expect(JSON.parse(String(calls[9]?.[1]?.body))).toMatchObject({
      ticket: "opaque-5",
      action: "revoked",
    });
  });
});

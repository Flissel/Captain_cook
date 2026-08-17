import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PortalPublicError, type PortalApi } from "../api";
import { FactoryActionInbox } from "./FactoryActionInbox";
import { clarificationAction, credentialAction, n8nApprovalAction } from "../test/fixtures";

function apiFixture(): PortalApi {
  return {
    getSetupSurface: vi.fn(),
    discoverCredentials: vi.fn(),
    selectCredential: vi.fn(),
    verifyCredential: vi.fn(),
    requestRotation: vi.fn(),
    revokeCredential: vi.fn(),
    getFactoryActions: vi.fn(async () => []),
    storeFactoryCredential: vi.fn(async () => "credential://tenant-a/n8n-salesforce"),
    respondToFactoryAction: vi.fn(async () => undefined),
  };
}

describe("FactoryActionInbox", () => {
  it("submits only the opaque credential reference with the action response", async () => {
    const user = userEvent.setup();
    const api = apiFixture();
    render(<FactoryActionInbox api={api} actions={[credentialAction]} getAccessToken={async () => "private-user-jwt"} onRefresh={vi.fn()} />);

    await user.type(screen.getByLabelText("Credential value"), "secret-value");
    await user.click(screen.getByRole("button", { name: /credential verbinden/i }));
    await user.click(screen.getByRole("button", { name: /fortsetzen/i }));

    expect(api.storeFactoryCredential).toHaveBeenCalledWith(
      credentialAction.requestId,
      "n8n.crm.read",
      "secret-value",
      "private-user-jwt",
    );
    expect(api.respondToFactoryAction).toHaveBeenCalledWith(
      credentialAction.requestId,
      expect.objectContaining({ credentialRef: "credential://tenant-a/n8n-salesforce" }),
      "private-user-jwt",
    );
    expect(JSON.stringify(vi.mocked(api.respondToFactoryAction).mock.calls)).not.toContain("secret-value");
    expect(screen.getByLabelText("Credential value")).toHaveValue("");
  });

  it("renders safe action state and only server-authorized decisions", () => {
    const api = apiFixture();
    render(
      <FactoryActionInbox
        api={api}
        getAccessToken={async () => "private-user-jwt"}
        onRefresh={vi.fn()}
        actions={[{ ...n8nApprovalAction, allowedDecisions: ["deny"], status: "pending" }, { ...clarificationAction, status: "expired" }]}
      />,
    );

    expect(screen.getByText("n8n.salesforce.write")).toBeVisible();
    expect(screen.getAllByText(/checkpoint\/aaaaaaaa/i)).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /approve n8n scope/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deny n8n scope/i })).toBeVisible();
    expect(screen.getByText("Expired")).toBeVisible();
  });

  it("sends a clarification answer with the fixed server scope", async () => {
    const user = userEvent.setup();
    const api = apiFixture();
    render(<FactoryActionInbox api={api} actions={[clarificationAction]} getAccessToken={async () => "private-user-jwt"} onRefresh={vi.fn()} />);

    await user.type(screen.getByLabelText("Clarification response"), "Focus on DACH enterprise accounts.");
    await user.click(screen.getByRole("button", { name: "Send clarification" }));
    expect(api.respondToFactoryAction).toHaveBeenCalledWith(
      clarificationAction.requestId,
      expect.objectContaining({
        approvedScope: clarificationAction.requiredScope,
        answerMetadata: { answer: "Focus on DACH enterprise accounts." },
      }),
      "private-user-jwt",
    );
  });

  it("refreshes instead of overwriting state after a stale response conflict", async () => {
    const user = userEvent.setup();
    const api = apiFixture();
    const refresh = vi.fn(async () => undefined);
    vi.mocked(api.respondToFactoryAction).mockRejectedValueOnce(new PortalPublicError("changed", 409));
    render(<FactoryActionInbox api={api} actions={[n8nApprovalAction]} getAccessToken={async () => "private-user-jwt"} onRefresh={refresh} />);

    await user.click(screen.getByRole("button", { name: /approve n8n scope/i }));
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  });
});

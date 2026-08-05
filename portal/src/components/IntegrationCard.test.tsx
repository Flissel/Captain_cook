import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { IntegrationCard } from "./IntegrationCard";
import { missingAction } from "../test/fixtures";
import type { IntegrationSetupStatus } from "../types";

const n8nCredentialsUrl = "https://n8n.example.test/home/credentials";

describe("IntegrationCard", () => {
  it("links safely to n8n and never renders credential-entry fields", () => {
    const { container } = render(
      <IntegrationCard
        action={missingAction}
        n8nCredentialsUrl={n8nCredentialsUrl}
        busy={false}
        onDiscover={vi.fn()}
        onSelect={vi.fn()}
        onVerify={vi.fn()}
        onRotate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByRole("link", { name: "Connect in n8n" })).toHaveAttribute(
      "href",
      n8nCredentialsUrl,
    );
    expect(screen.getByRole("link", { name: "Connect in n8n" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    expect(container.querySelectorAll("input")).toHaveLength(0);
    expect(container.textContent).not.toMatch(/secret|password|access.?token|refresh.?token/i);
  });

  it.each<IntegrationSetupStatus>([
    "missing",
    "selection_required",
    "verification_required",
    "verification_failed",
    "ready",
    "revoked",
    "expired",
  ])("renders the %s status explicitly", (status) => {
    render(
      <IntegrationCard
        action={{ ...missingAction, status }}
        n8nCredentialsUrl={n8nCredentialsUrl}
        busy={false}
        onDiscover={vi.fn()}
        onSelect={vi.fn()}
        onVerify={vi.fn()}
        onRotate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByTestId("setup-status")).toHaveAttribute("data-status", status);
  });

  it("selects discovered credential metadata without exposing values", async () => {
    const onSelect = vi.fn(async () => undefined);
    render(
      <IntegrationCard
        action={{
          ...missingAction,
          status: "selection_required",
          candidateCredentials: [
            {
              credentialId: "credential-1",
              credentialName: "CRM Primary",
              credentialType: "hubspotApi",
              projectId: "project-1",
              projectName: "Sales",
            },
          ],
        }}
        n8nCredentialsUrl={n8nCredentialsUrl}
        busy={false}
        onDiscover={vi.fn()}
        onSelect={onSelect}
        onVerify={vi.fn()}
        onRotate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Use CRM Primary" }));
    expect(onSelect).toHaveBeenCalledWith("credential-1");
  });

  it("disables actions while a mutation is running", () => {
    render(
      <IntegrationCard
        action={{ ...missingAction, status: "ready" }}
        n8nCredentialsUrl={n8nCredentialsUrl}
        busy
        onDiscover={vi.fn()}
        onSelect={vi.fn()}
        onVerify={vi.fn()}
        onRotate={vi.fn()}
        onRevoke={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Updating setup");
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
  });

  it.each(["verification_required", "verification_failed", "expired"] as const)(
    "offers verification for %s without credential input fields",
    async (status) => {
      const onVerify = vi.fn(async () => undefined);
      const { container } = render(
        <IntegrationCard
          action={{
            ...missingAction,
            status,
            selectedCredential: {
              credentialId: "credential-1",
              credentialName: "CRM Primary",
              credentialType: "hubspotApi",
              projectId: null,
              projectName: null,
            },
          }}
          n8nCredentialsUrl={n8nCredentialsUrl}
          busy={false}
          onDiscover={vi.fn()}
          onSelect={vi.fn()}
          onVerify={onVerify}
          onRotate={vi.fn()}
          onRevoke={vi.fn()}
        />,
      );

      await userEvent.click(screen.getByRole("button", { name: "Verify connection" }));
      expect(onVerify).toHaveBeenCalledOnce();
      expect(container.querySelectorAll("input")).toHaveLength(0);
    },
  );
});

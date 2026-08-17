import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { credentialAction } from "./test/fixtures";
import type { FactoryActionRequest, FactoryActionStatus } from "./types";
import type { PortalApi } from "./api";

const mocks = vi.hoisted(() => ({
  getSetupSurface: vi.fn(),
  signInWithPassword: vi.fn(async () => ({ error: null })),
  unsubscribe: vi.fn(),
  getFactoryActions: vi.fn<(accessToken: string, status?: FactoryActionStatus) => Promise<FactoryActionRequest[]>>(async () => []),
  session: null as { access_token: string } | null,
}));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    createPortalApi: () => ({
      getSetupSurface: mocks.getSetupSurface,
      discoverCredentials: vi.fn(),
      selectCredential: vi.fn(),
      verifyCredential: vi.fn(),
      requestRotation: vi.fn(),
      revokeCredential: vi.fn(),
      getFactoryActions: mocks.getFactoryActions,
      storeFactoryCredential: vi.fn(),
      respondToFactoryAction: vi.fn(),
    }),
  };
});

vi.mock("./supabase", () => ({
  portalSupabase: () => ({
    auth: {
      getSession: vi.fn(async () => ({ data: { session: mocks.session }, error: null })),
      onAuthStateChange: vi.fn(() => ({ data: { subscription: { unsubscribe: mocks.unsubscribe } } })),
      signInWithPassword: mocks.signInWithPassword,
      signOut: vi.fn(),
    },
  }),
}));

import App, { PortalDashboard } from "./App";

beforeEach(() => {
  vi.clearAllMocks();
  mocks.session = null;
});

it("loads authenticated factory actions without replacing Integration Setup", async () => {
  mocks.session = { access_token: "private-user-jwt" };
  mocks.getFactoryActions.mockImplementation(async (accessToken, status) => {
    expect(accessToken).toBe("private-user-jwt");
    return status === "pending" ? [credentialAction] : [];
  });

  render(<App />);

  expect(await screen.findByRole("heading", { name: "Factory actions" })).toBeVisible();
  expect(screen.getByText("A CRM credential is required.")).toBeVisible();
  expect(mocks.getFactoryActions).toHaveBeenCalledWith("private-user-jwt", "pending");
  expect(mocks.getFactoryActions).toHaveBeenCalledWith("private-user-jwt", "denied");
});

function actionApi(getFactoryActions: PortalApi["getFactoryActions"]): PortalApi {
  return {
    getSetupSurface: vi.fn(),
    discoverCredentials: vi.fn(),
    selectCredential: vi.fn(),
    verifyCredential: vi.fn(),
    requestRotation: vi.fn(),
    revokeCredential: vi.fn(),
    getFactoryActions,
    storeFactoryCredential: vi.fn(),
    respondToFactoryAction: vi.fn(),
  };
}

it("keeps the newest factory-action page when earlier loads finish out of order", async () => {
  mocks.session = { access_token: "private-user-jwt" };
  const pendingResolvers: Array<() => void> = [];
  const firstApi = actionApi(() => new Promise<FactoryActionRequest[]>((resolve) => {
    pendingResolvers.push(() => resolve([credentialAction]));
  }));
  const newerAction = { ...credentialAction, message: "Newer authoritative action." };
  const secondApi = actionApi(async (_accessToken, status) => status === "pending" ? [newerAction] : []);
  const view = render(<PortalDashboard api={firstApi} />);

  await waitFor(() => expect(pendingResolvers).toHaveLength(5));
  view.rerender(<PortalDashboard api={secondApi} />);
  expect(await screen.findByText("Newer authoritative action.")).toBeVisible();
  for (const resolve of pendingResolvers) resolve();
  await waitFor(() => expect(screen.queryByText("A CRM credential is required.")).not.toBeInTheDocument());
  expect(screen.getByText("Newer authoritative action.")).toBeVisible();
});

it("does not call Gateway while the real App is unauthenticated", async () => {
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Connect your workspace" })).toBeVisible();
  expect(mocks.getSetupSurface).not.toHaveBeenCalled();
});

it("signs an existing Supabase user in with email and password", async () => {
  render(<App />);
  const email = await screen.findByLabelText("Work email");
  const password = await screen.findByLabelText("Password");

  await userEvent.type(email, "operator@example.test");
  await userEvent.type(password, "correct-horse-battery-staple");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

  await waitFor(() =>
    expect(mocks.signInWithPassword).toHaveBeenCalledWith({
      email: "operator@example.test",
      password: "correct-horse-battery-staple",
    }),
  );
  expect(mocks.getSetupSurface).not.toHaveBeenCalled();
});

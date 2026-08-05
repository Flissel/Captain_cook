import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getSetupSurface: vi.fn(),
  signInWithPassword: vi.fn(async () => ({ error: null })),
  unsubscribe: vi.fn(),
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
    }),
  };
});

vi.mock("./supabase", () => ({
  portalSupabase: () => ({
    auth: {
      getSession: vi.fn(async () => ({ data: { session: null }, error: null })),
      onAuthStateChange: vi.fn(() => ({ data: { subscription: { unsubscribe: mocks.unsubscribe } } })),
      signInWithPassword: mocks.signInWithPassword,
      signOut: vi.fn(),
    },
  }),
}));

import App from "./App";

beforeEach(() => {
  vi.clearAllMocks();
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

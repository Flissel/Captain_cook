import { afterEach, describe, expect, it, vi } from "vitest";
import { portalPublicConfig } from "./config";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("portal browser configuration", () => {
  it("contains only Supabase public identity configuration", () => {
    vi.stubEnv("VITE_SUPABASE_URL", "https://identity.example.test");
    vi.stubEnv("VITE_SUPABASE_ANON_KEY", "public-anon-value");
    vi.stubEnv("VITE_CAPTAIN_PORTAL_API_BASE_URL", "https://public-or-lan-target.invalid");

    expect(portalPublicConfig()).toEqual({
      supabaseUrl: "https://identity.example.test",
      supabaseAnonKey: "public-anon-value",
    });
  });

  it("uses a fixed same-origin portal API prefix", async () => {
    const fetcher = vi.fn(async () => new Response(null, { status: 503 }));
    const { createPortalApi } = await import("./api");
    const api = createPortalApi(fetcher);

    await expect(
      api.getSetupSurface("10000000-0000-0000-0000-000000000001", "browser-session"),
    ).rejects.toBeDefined();
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/portal/integration-setups/10000000-0000-0000-0000-000000000001",
      expect.any(Object),
    );
  });
});

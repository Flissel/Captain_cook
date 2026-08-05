import { expect, it, vi } from "vitest";
import { loadSurfaceForSession } from "./auth";

it("does not call Gateway without an authenticated Supabase session", async () => {
  const getSetupSurface = vi.fn();
  await expect(loadSurfaceForSession(null, "job-a", { getSetupSurface })).resolves.toBeNull();
  expect(getSetupSurface).not.toHaveBeenCalled();
});

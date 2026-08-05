import type { Session } from "@supabase/supabase-js";
import type { PortalApi } from "./api";
import type { PortalSetupSurface } from "./types";

export async function loadSurfaceForSession(
  session: Pick<Session, "access_token"> | null,
  jobId: string,
  api: Pick<PortalApi, "getSetupSurface">,
): Promise<PortalSetupSurface | null> {
  if (session === null) {
    return null;
  }
  return api.getSetupSurface(jobId, session.access_token);
}

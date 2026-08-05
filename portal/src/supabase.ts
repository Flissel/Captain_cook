import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { portalPublicConfig } from "./config";

let client: SupabaseClient | null = null;

export function portalSupabase(): SupabaseClient {
  if (client === null) {
    const config = portalPublicConfig();
    client = createClient(config.supabaseUrl, config.supabaseAnonKey);
  }
  return client;
}

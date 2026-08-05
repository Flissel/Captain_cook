interface PortalPublicConfig {
  supabaseUrl: string;
  supabaseAnonKey: string;
}

function requiredPublicValue(name: string, value: string | undefined): string {
  if (value === undefined || value.trim() === "") {
    throw new Error(`Missing public portal configuration: ${name}`);
  }
  return value;
}

export function portalPublicConfig(): PortalPublicConfig {
  return {
    supabaseUrl: requiredPublicValue("VITE_SUPABASE_URL", import.meta.env.VITE_SUPABASE_URL),
    supabaseAnonKey: requiredPublicValue("VITE_SUPABASE_ANON_KEY", import.meta.env.VITE_SUPABASE_ANON_KEY),
  };
}

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { createPortalApi, PortalPublicError, type PortalApi } from "./api";
import { IntegrationCard } from "./components/IntegrationCard";
import { SetupStatus } from "./components/SetupStatus";
import { portalSupabase } from "./supabase";
import type { PortalSetupSurface } from "./types";

const genericMessage = "The integration request could not be completed.";

function publicMessage(error: unknown): string {
  return error instanceof PortalPublicError ? error.message : genericMessage;
}

async function currentAccessToken(): Promise<string> {
  const { data, error } = await portalSupabase().auth.getSession();
  if (error || data.session === null) {
    throw new PortalPublicError("Your session has expired. Sign in again.");
  }
  return data.session.access_token;
}

function SignIn() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setMessage(null);
    const { error } = await portalSupabase().auth.signInWithPassword({
      email,
      password,
    });
    setMessage(error ? "Sign-in failed." : null);
    setPending(false);
  }

  return (
    <main className="auth-shell">
      <section className="auth-panel">
        <div className="brand-mark" aria-hidden="true">C</div>
        <p className="eyebrow">Captain integration control</p>
        <h1>Connect your workspace</h1>
        <p className="intro">Use your organization account. Connection details stay in n8n.</p>
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor="email">Work email</label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <button disabled={pending} type="submit">
            {pending ? "Signing in…" : "Sign in"}
          </button>
        </form>
        {message && <p role="status" className="notice">{message}</p>}
      </section>
    </main>
  );
}

function PortalDashboard({ api }: { api: PortalApi }) {
  const [jobInput, setJobInput] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [surface, setSurface] = useState<PortalSetupSurface | null>(null);
  const [busyAlias, setBusyAlias] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading || busyAlias !== null) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.getSetupSurface(jobInput.trim(), await currentAccessToken());
      setSurface(result);
      setJobId(jobInput.trim());
    } catch (caught) {
      setSurface(null);
      setError(publicMessage(caught));
    } finally {
      setLoading(false);
    }
  }

  async function mutate(
    alias: string,
    operation: (resolvedJobId: string, accessToken: string) => Promise<PortalSetupSurface>,
  ) {
    if (busyAlias !== null || jobId === null) return;
    setBusyAlias(alias);
    setError(null);
    try {
      setSurface(await operation(jobId, await currentAccessToken()));
    } catch (caught) {
      setError(publicMessage(caught));
    } finally {
      setBusyAlias(null);
    }
  }

  return (
    <main className="portal-shell">
      <header className="portal-header">
        <div>
          <p className="eyebrow">Captain integration control</p>
          <h1>Connection readiness</h1>
        </div>
        <button className="quiet" type="button" onClick={() => void portalSupabase().auth.signOut()}>
          Sign out
        </button>
      </header>

      <section className="lookup-panel" aria-labelledby="lookup-title">
        <div>
          <h2 id="lookup-title">Open a setup</h2>
          <p>Use the setup reference supplied by Captain.</p>
        </div>
        <form onSubmit={(event) => void load(event)}>
          <label htmlFor="job-id">Setup reference</label>
          <div className="lookup-row">
            <input
              id="job-id"
              required
              value={jobInput}
              onChange={(event) => setJobInput(event.target.value)}
            />
            <button disabled={loading || busyAlias !== null} type="submit">
              {loading ? "Loading…" : "Open setup"}
            </button>
          </div>
        </form>
      </section>

      {error && <p role="alert" className="error-banner">{error}</p>}
      {surface && (
        <section className="setup-grid" aria-label="Integration setup">
          <div className="surface-summary">
            <span>Overall readiness</span>
            <SetupStatus status={surface.overallStatus} />
          </div>
          {surface.actions.map((action) => (
            <IntegrationCard
              key={action.credentialAlias}
              action={action}
              n8nCredentialsUrl={surface.n8nCredentialsUrl}
              busy={busyAlias !== null}
              onDiscover={() =>
                mutate(action.credentialAlias, (resolvedJobId, accessToken) =>
                  api.discoverCredentials(resolvedJobId, action.credentialAlias, accessToken),
                )
              }
              onSelect={(credentialId) =>
                mutate(action.credentialAlias, (resolvedJobId, accessToken) =>
                  api.selectCredential(resolvedJobId, action.credentialAlias, credentialId, accessToken),
                )
              }
              onVerify={() =>
                mutate(action.credentialAlias, (resolvedJobId, accessToken) =>
                  api.verifyCredential(resolvedJobId, action.credentialAlias, accessToken),
                )
              }
              onRotate={() =>
                mutate(action.credentialAlias, (resolvedJobId, accessToken) =>
                  api.requestRotation(resolvedJobId, action.credentialAlias, accessToken),
                )
              }
              onRevoke={() =>
                mutate(action.credentialAlias, (resolvedJobId, accessToken) =>
                  api.revokeCredential(resolvedJobId, action.credentialAlias, accessToken),
                )
              }
            />
          ))}
        </section>
      )}
    </main>
  );
}

export default function App() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const api = useMemo(() => createPortalApi(), []);

  useEffect(() => {
    let active = true;
    void portalSupabase().auth.getSession().then(({ data }) => {
      if (active) setSignedIn(data.session !== null);
    });
    const { data } = portalSupabase().auth.onAuthStateChange((_event, session) => {
      if (active) setSignedIn(session !== null);
    });
    return () => {
      active = false;
      data.subscription.unsubscribe();
    };
  }, []);

  if (signedIn === null) {
    return <main className="loading-shell"><p role="status">Checking your session…</p></main>;
  }
  return signedIn ? <PortalDashboard api={api} /> : <SignIn />;
}

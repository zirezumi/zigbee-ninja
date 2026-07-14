import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, BrokerView } from "./api";
import type { Health, Me } from "./api";
import BrokerSetup from "./views/BrokerSetup";
import Fleet from "./views/Fleet";

type Phase = "loading" | "setup" | "login" | "ready" | "error";

const NAV_ITEMS = [
  "Fleet",
  "Attribution",
  "Burst inspector",
  "Topology",
  "Calibration",
  "Footprint",
  "Alerts",
  "Settings",
];

interface CredentialsFormProps {
  title: string;
  subtitle: string;
  submitLabel: string;
  onSubmit: (username: string, password: string) => Promise<void>;
}

function CredentialsForm({ title, subtitle, submitLabel, onSubmit }: CredentialsFormProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSubmit(username, password);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to reach the collector");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <form className="card" onSubmit={(event) => void handleSubmit(event)}>
        <div className="brand">
          <span className="brand-mark">🥷</span>
          <span className="brand-name">zigbee-ninja</span>
        </div>
        <h1>{title}</h1>
        <p className="subtitle">{subtitle}</p>
        <label>
          Username
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? "…" : submitLabel}
        </button>
      </form>
    </div>
  );
}

export default function App() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [version, setVersion] = useState("");
  const [username, setUsername] = useState("");
  const [broker, setBroker] = useState<BrokerView | null>(null);
  const [reconfiguring, setReconfiguring] = useState(false);

  const refreshBroker = useCallback(async () => {
    setBroker(await api<BrokerView>("/api/broker"));
  }, []);

  const bootstrap = useCallback(async () => {
    try {
      const health = await api<Health>("/api/health");
      setVersion(health.version);
      if (!health.setup_complete) {
        setPhase("setup");
        return;
      }
      try {
        const me = await api<Me>("/api/auth/me");
        setUsername(me.username);
        await refreshBroker();
        setPhase("ready");
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          setPhase("login");
        } else {
          throw err;
        }
      }
    } catch {
      setPhase("error");
    }
  }, [refreshBroker]);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  async function handleSetup(user: string, password: string) {
    const created = await api<Me>("/api/setup", {
      method: "POST",
      body: JSON.stringify({ username: user, password }),
    });
    setUsername(created.username);
    await refreshBroker();
    setPhase("ready");
  }

  async function handleLogin(user: string, password: string) {
    const loggedIn = await api<Me>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: user, password }),
    });
    setUsername(loggedIn.username);
    await refreshBroker();
    setPhase("ready");
  }

  async function handleLogout() {
    await api("/api/auth/logout", { method: "POST" });
    setPhase("login");
  }

  if (phase === "loading") {
    return <div className="gate quiet">connecting…</div>;
  }
  if (phase === "error") {
    return (
      <div className="gate quiet">
        <div>
          <p>Can't reach the collector API.</p>
          <button onClick={() => void bootstrap()}>Retry</button>
        </div>
      </div>
    );
  }
  if (phase === "setup") {
    return (
      <CredentialsForm
        title="Create the admin account"
        subtitle="First run: choose the credentials that will control probe deployment."
        submitLabel="Create account"
        onSubmit={handleSetup}
      />
    );
  }
  if (phase === "login") {
    return (
      <CredentialsForm
        title="Sign in"
        subtitle="The collector is running and set up."
        submitLabel="Sign in"
        onSubmit={handleLogin}
      />
    );
  }

  const showSetup = broker !== null && (!broker.configured || reconfiguring);

  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <span className="brand-mark">🥷</span>
          <span className="brand-name">zigbee-ninja</span>
        </div>
        <nav>
          {NAV_ITEMS.map((item, index) => (
            <span key={item} className={index === 0 ? "nav-item active" : "nav-item disabled"}>
              {item}
            </span>
          ))}
        </nav>
        <div className="aside-foot">
          <span className="mono">{version}</span>
          <button className="ghost" onClick={() => void handleLogout()}>
            Sign out {username}
          </button>
        </div>
      </aside>
      <main>
        <h1>Fleet</h1>
        {broker === null ? (
          <p className="hint">loading…</p>
        ) : showSetup ? (
          <BrokerSetup
            current={broker.configured ? broker : null}
            onSaved={() => {
              setReconfiguring(false);
              void refreshBroker();
            }}
          />
        ) : (
          <Fleet onReconfigure={() => setReconfiguring(true)} />
        )}
      </main>
    </div>
  );
}

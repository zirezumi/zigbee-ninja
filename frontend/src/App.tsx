import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, BrokerView } from "./api";
import type { Health, Me } from "./api";
import Alerts from "./views/Alerts";
import Attribution from "./views/Attribution";
import BrokerSetup from "./views/BrokerSetup";
import Burst from "./views/Burst";
import Calibration from "./views/Calibration";
import Fleet from "./views/Fleet";
import Footprint from "./views/Footprint";
import Headroom from "./views/Headroom";
import Rebalance from "./views/Rebalance";
import Recommendations from "./views/Recommendations";
import Settings from "./views/Settings";
import Topology from "./views/Topology";
import Wire from "./views/Wire";

type Phase = "loading" | "setup" | "login" | "ready" | "error";
type View =
  | "fleet"
  | "attribution"
  | "wiretap"
  | "headroom"
  | "recommendations"
  | "rebalance"
  | "topology"
  | "calibration"
  | "permissions"
  | "alerts"
  | "settings"
  | "benchmark";
// The broker-connection form is a route of its own so the browser's back
// button leaves it, and every view survives a page refresh via the URL hash.
type Route = View | "broker";

const ROUTES: ReadonlySet<string> = new Set([
  "fleet",
  "attribution",
  "wiretap",
  "headroom",
  "recommendations",
  "rebalance",
  "benchmark",
  "topology",
  "calibration",
  "permissions",
  "alerts",
  "settings",
  "broker",
]);

// Hashes from before the views took their current names keep working.
const LEGACY_ROUTES: Record<string, Route> = {
  wire: "wiretap",
  burst: "benchmark",
  footprint: "permissions",
};

function routeFromHash(): Route {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash in LEGACY_ROUTES) return LEGACY_ROUTES[hash];
  return ROUTES.has(hash) ? (hash as Route) : "fleet";
}

function navigate(route: Route) {
  window.location.hash = `/${route}`;
}

// Theme: "auto" follows the operating system; light/dark force a palette.
type Theme = "auto" | "light" | "dark";
const THEME_ORDER: Theme[] = ["auto", "light", "dark"];

function applyTheme(theme: Theme) {
  if (theme === "auto") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", theme);
  }
}

function storedTheme(): Theme {
  const saved = window.localStorage.getItem("zn-theme");
  return saved === "light" || saved === "dark" ? saved : "auto";
}

const NAV_ITEMS: Array<{ label: string; view?: View }> = [
  { label: "Fleet", view: "fleet" },
  { label: "Attribution", view: "attribution" },
  { label: "Wiretap", view: "wiretap" },
  { label: "Headroom", view: "headroom" },
  { label: "Recommendations", view: "recommendations" },
  { label: "Rebalance", view: "rebalance" },
  { label: "Benchmark", view: "benchmark" },
  { label: "Topology", view: "topology" },
  { label: "Calibration", view: "calibration" },
  { label: "Permissions", view: "permissions" },
  { label: "Alerts", view: "alerts" },
  { label: "Settings", view: "settings" },
];

const VIEW_TITLES: Record<View, string> = {
  fleet: "Fleet",
  attribution: "Attribution",
  wiretap: "Wiretap",
  headroom: "Headroom",
  recommendations: "Recommendations",
  rebalance: "Rebalance",
  topology: "Topology",
  calibration: "Calibration",
  permissions: "Permissions",
  alerts: "Alerts",
  settings: "Settings",
  benchmark: "Benchmark",
};

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
          <img className="brand-mark" src="/logo.png" alt="" width={24} height={24} />
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
  const [route, setRoute] = useState<Route>(routeFromHash);
  const [theme, setTheme] = useState<Theme>(storedTheme);

  useEffect(() => {
    const onHashChange = () => setRoute(routeFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    applyTheme(theme);
    window.localStorage.setItem("zn-theme", theme);
  }, [theme]);

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

  const showSetup = broker !== null && (!broker.configured || route === "broker");
  const view: View = route === "broker" ? "fleet" : route;

  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <img className="brand-mark brand-mark-lg" src="/logo.png" alt="zigbee-ninja" width={44} height={44} />
        </div>
        <nav>
          {NAV_ITEMS.map((item) =>
            item.view ? (
              <button
                key={item.label}
                className={item.view === route ? "nav-item active" : "nav-item"}
                onClick={() => navigate(item.view!)}
              >
                {item.label}
              </button>
            ) : (
              <span key={item.label} className="nav-item disabled">
                {item.label}
              </span>
            ),
          )}
        </nav>
        <div className="aside-foot">
          <span className="mono">{version}</span>
          <button
            className="ghost"
            title="System follows your operating system's light/dark setting"
            onClick={() =>
              setTheme(THEME_ORDER[(THEME_ORDER.indexOf(theme) + 1) % THEME_ORDER.length])
            }
          >
            Theme: {theme === "auto" ? "system" : theme}
          </button>
          <button className="ghost" onClick={() => void handleLogout()}>
            Sign out {username}
          </button>
        </div>
      </aside>
      <main>
        <h1>{showSetup ? "Broker connection" : VIEW_TITLES[view]}</h1>
        {broker === null ? (
          <p className="hint">loading…</p>
        ) : showSetup ? (
          <BrokerSetup
            current={broker.configured ? broker : null}
            onSaved={() => {
              if (route === "broker") navigate("fleet");
              void refreshBroker();
            }}
            onCancel={broker.configured ? () => navigate("fleet") : undefined}
          />
        ) : view === "fleet" ? (
          <Fleet onReconfigure={() => navigate("broker")} brokerInfo={broker} />
        ) : view === "attribution" ? (
          <Attribution />
        ) : view === "wiretap" ? (
          <Wire />
        ) : view === "headroom" ? (
          <Headroom />
        ) : view === "recommendations" ? (
          <Recommendations />
        ) : view === "rebalance" ? (
          <Rebalance />
        ) : view === "topology" ? (
          <Topology />
        ) : view === "calibration" ? (
          <Calibration />
        ) : view === "alerts" ? (
          <Alerts />
        ) : view === "settings" ? (
          <Settings />
        ) : view === "benchmark" ? (
          <Burst />
        ) : (
          <Footprint />
        )}
      </main>
    </div>
  );
}

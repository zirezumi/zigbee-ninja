import { FormEvent, useState } from "react";
import { api, ApiError, BrokerView } from "../api";

interface BrokerSetupProps {
  current?: BrokerView | null;
  onSaved: () => void;
}

export default function BrokerSetup({ current, onSaved }: BrokerSetupProps) {
  const [host, setHost] = useState(current?.host ?? "");
  const [port, setPort] = useState(String(current?.port ?? 1883));
  const [username, setUsername] = useState(current?.username ?? "");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/broker", {
        method: "POST",
        body: JSON.stringify({
          host,
          port: Number(port) || 1883,
          username: username || null,
          password: password || null,
        }),
      });
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to reach the collector");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel narrow">
      <p className="panel-kicker">Connect your MQTT broker</p>
      <p className="hint">
        The broker is the only mandatory contact point: every Zigbee2MQTT instance announces
        itself on retained <code>bridge</code> topics, so discovery starts the moment the
        connection is up. The connection is tested before saving.
      </p>
      <form onSubmit={(event) => void handleSubmit(event)} className="stack">
        <div className="row">
          <label className="grow">
            Host
            <input
              value={host}
              onChange={(event) => setHost(event.target.value)}
              placeholder="mqtt.local or 10.0.0.5"
              required
              autoFocus
            />
          </label>
          <label className="port">
            Port
            <input
              value={port}
              onChange={(event) => setPort(event.target.value)}
              inputMode="numeric"
              pattern="[0-9]*"
            />
          </label>
        </div>
        <label>
          Username <span className="opt">(optional)</span>
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label>
          Password <span className="opt">(optional)</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="off"
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy || !host}>
          {busy ? "Testing connection…" : "Connect"}
        </button>
      </form>
    </div>
  );
}

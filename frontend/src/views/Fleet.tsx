import { useEffect, useRef, useState } from "react";
import { FleetMessage, InstanceInfo, fleetSocketUrl } from "../api";

const KIND_ORDER = ["command", "state", "bridge", "availability", "probe", "other"];
const HISTORY_LENGTH = 60;

function totalPerSecond(kinds: Record<string, number> | undefined): number {
  if (!kinds) return 0;
  return Object.entries(kinds)
    .filter(([kind]) => kind !== "total_60s")
    .reduce((sum, [, count]) => sum + count, 0);
}

function Sparkline({ values }: { values: number[] }) {
  const width = 140;
  const height = 30;
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((value, index) => `${(index * step).toFixed(1)},${(height - 2 - (value / max) * (height - 4)).toFixed(1)}`)
    .join(" ");
  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      {values.length > 1 && <polyline points={points} />}
    </svg>
  );
}

interface InstanceCardProps {
  instance: InstanceInfo;
  kinds: Record<string, number> | undefined;
  history: number[];
}

function InstanceCard({ instance, kinds, history }: InstanceCardProps) {
  const online = instance.online;
  return (
    <div className="instance-card">
      <div className="instance-head">
        <span className={online === false ? "dot off" : online ? "dot on" : "dot unknown"} />
        <span className="instance-name">{instance.base_topic}</span>
        <span className="rate-big">
          {totalPerSecond(kinds)}
          <span className="rate-unit">msg/s</span>
        </span>
      </div>
      <Sparkline values={history} />
      <div className="kv-grid">
        <span>Z2M</span>
        <span>{instance.version ?? "—"}</span>
        <span>Channel</span>
        <span>{instance.channel ?? "—"}</span>
        <span>Adapter</span>
        <span className="clip" title={instance.adapter_port ?? undefined}>
          {instance.coordinator_type ?? "—"}
          {instance.adapter_port ? ` · ${instance.adapter_port}` : ""}
        </span>
        <span>Devices</span>
        <span>
          {instance.device_count} ({instance.router_count} routers,{" "}
          {instance.end_device_count} end devices)
        </span>
        <span>Groups</span>
        <span>{instance.group_count}</span>
      </div>
      <div className="kinds">
        {KIND_ORDER.map((kind) => (
          <span key={kind} className="kind">
            <span className="kind-name">{kind}</span>
            <span className="kind-count">{kinds?.[kind] ?? 0}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

interface FleetProps {
  onReconfigure: () => void;
}

export default function Fleet({ onReconfigure }: FleetProps) {
  const [message, setMessage] = useState<FleetMessage | null>(null);
  const [socketState, setSocketState] = useState<"connecting" | "open" | "closed">("connecting");
  const historyRef = useRef<Record<string, number[]>>({});

  useEffect(() => {
    let alive = true;
    let socket: WebSocket | null = null;
    let retry: number | undefined;

    function connect() {
      if (!alive) return;
      setSocketState("connecting");
      socket = new WebSocket(fleetSocketUrl());
      socket.onopen = () => alive && setSocketState("open");
      socket.onmessage = (event) => {
        if (!alive) return;
        const parsed = JSON.parse(event.data as string) as FleetMessage;
        const keys = [...parsed.instances.map((instance) => instance.base_topic), "*"];
        for (const key of keys) {
          const history = historyRef.current[key] ?? [];
          history.push(totalPerSecond(parsed.rates[key]));
          historyRef.current[key] = history.slice(-HISTORY_LENGTH);
        }
        setMessage(parsed);
      };
      socket.onclose = () => {
        if (!alive) return;
        setSocketState("closed");
        retry = window.setTimeout(connect, 2000);
      };
    }

    connect();
    return () => {
      alive = false;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, []);

  const broker = message?.broker;
  const instances = message?.instances ?? [];
  const globalRate = totalPerSecond(message?.rates["*"]);

  return (
    <>
      <div className={`banner ${broker?.state === "connected" ? "ok" : "warn"}`}>
        <span>
          Broker: <strong>{broker?.state ?? socketState}</strong>
          {broker?.error ? ` — ${broker.error}` : ""}
          {broker?.state === "connected" ? ` · ${globalRate} msg/s across the broker` : ""}
        </span>
        <button className="ghost small" onClick={onReconfigure}>
          Reconfigure
        </button>
      </div>

      {instances.length === 0 ? (
        <div className="panel">
          <p className="panel-kicker">Waiting for discovery</p>
          <p className="hint">
            Connected instances announce themselves on retained <code>&lt;base&gt;/bridge/info</code>{" "}
            topics, which arrive as soon as the subscription is up. If nothing appears within a few
            seconds, the account may lack read access to the Zigbee2MQTT base topics.
          </p>
        </div>
      ) : (
        <div className="cards">
          {instances.map((instance) => (
            <InstanceCard
              key={instance.base_topic}
              instance={instance}
              kinds={message?.rates[instance.base_topic]}
              history={historyRef.current[instance.base_topic] ?? []}
            />
          ))}
        </div>
      )}
    </>
  );
}

import { Card } from "@/components/ui/Card";
import { StatusBadge } from "@/components/ui/StatusBadge";
import type { Health } from "@/lib/types";

const STATE_LABELS: Record<string, string> = {
  ok: "Connected",
  unavailable: "Unavailable",
  not_configured: "Not configured",
};

const LABELS: Record<string, string> = {
  database: "PostgreSQL",
  claude: "Claude agent",
  gmail: "Gmail",
};

export function SystemStatus({ health }: { health: Health | null }) {
  return (
    <Card title="System" description={health ? `API v${health.version}` : "Backend unreachable"}>
      <ul className="flex flex-col divide-y divide-border">
        <li className="flex items-center justify-between py-2 first:pt-0">
          <span className="text-sm text-ink-2">Backend API</span>
          <StatusBadge
            status={health ? (health.status === "ok" ? "ok" : "PAUSED") : "unavailable"}
            label={health ? (health.status === "ok" ? "Online" : "Degraded") : "Offline"}
          />
        </li>
        {health &&
          Object.entries(health.components).map(([name, c]) => (
            <li key={name} className="flex items-center justify-between py-2 last:pb-0">
              <span className="text-sm text-ink-2">{LABELS[name] ?? name}</span>
              <StatusBadge status={c.status} label={STATE_LABELS[c.status]} />
            </li>
          ))}
      </ul>
      {!health && (
        <p className="mt-3 text-xs text-ink-3">
          Start it with <code className="rounded bg-surface-2 px-1 py-0.5 font-mono">uvicorn backend.main:app</code>.
        </p>
      )}
    </Card>
  );
}

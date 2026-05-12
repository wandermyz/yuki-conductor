import { useEffect, useState } from "react";

interface PluginStatus {
  status: "ok" | "error" | "auth_pending" | "stopped";
  message: string;
  details?: Record<string, unknown>;
}

const badgeColors: Record<string, string> = {
  ok: "#22c55e",
  error: "#ef4444",
  auth_pending: "#eab308",
  stopped: "#6b7280",
};

export default function StatusView() {
  const [statuses, setStatuses] = useState<Record<string, PluginStatus>>({});

  const fetchStatus = () => {
    fetch("/api/status")
      .then((r) => r.json())
      .then(setStatuses)
      .catch(console.error);
  };

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 10000);
    return () => clearInterval(id);
  }, []);

  const entries = Object.entries(statuses);

  return (
    <div style={{ padding: "1.5rem", maxWidth: 720 }}>
      <h2 style={{ marginBottom: "1rem" }}>Plugin Status</h2>
      {entries.length === 0 && <p>No plugins registered.</p>}
      {entries.map(([name, s]) => (
        <div
          key={name}
          style={{
            border: "1px solid #333",
            borderRadius: 8,
            padding: "1rem",
            marginBottom: "0.75rem",
            background: "#1a1a1a",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            <span
              style={{
                display: "inline-block",
                width: 10,
                height: 10,
                borderRadius: "50%",
                background: badgeColors[s.status] ?? "#6b7280",
              }}
            />
            <strong>{name}</strong>
            <span style={{ color: "#888", fontSize: "0.85em" }}>{s.status}</span>
          </div>
          <pre style={{ margin: 0, whiteSpace: "pre-wrap", color: "#ccc", fontSize: "0.9em" }}>
            {s.message}
          </pre>
        </div>
      ))}
    </div>
  );
}

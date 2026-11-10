/** Typed client for `/api/heartbeats` (src/yuki_conductor/heartbeat_routes.py). */

export type HeartbeatKind = "conversation" | "task";

export interface Heartbeat {
  target_kind: HeartbeatKind;
  target_id: string;
  prompt: string;
  interval_s: number;
  next_due_at: number;
  expires_at: number;
  paused: boolean;
  created_by: "agent" | "user";
  created_at: number;
  last_tick_at: number | null;
  last_outcome: "quiet" | "notified" | "error" | null;
  consecutive_errors: number;
  /** A tick is running on the target right now. */
  ticking: boolean;
}

export interface HeartbeatTick {
  id: number;
  started_at: number;
  finished_at: number | null;
  outcome: "quiet" | "notified" | "error" | null;
  response: string | null;
  error: string | null;
}

export const DEFAULT_INTERVAL_S = 270;
export const MIN_INTERVAL_S = 60;

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    const data = await r.json().catch(() => null);
    throw new Error(typeof data?.detail === "string" ? data.detail : `${r.status}`);
  }
  return r.json();
}

const path = (kind: HeartbeatKind, id: string | number) => `/api/heartbeats/${kind}/${id}`;

export const getHeartbeat = (kind: HeartbeatKind, id: string | number) =>
  call<{ heartbeat: Heartbeat | null; ticks: HeartbeatTick[] }>("GET", path(kind, id));
export const setHeartbeat = (kind: HeartbeatKind, id: string | number, prompt: string, interval_s: number) =>
  call<Heartbeat>("PUT", path(kind, id), { prompt, interval_s, created_by: "user" });
export const updateHeartbeat = (
  kind: HeartbeatKind,
  id: string | number,
  fields: { prompt?: string; interval_s?: number; paused?: boolean },
) => call<Heartbeat>("PATCH", path(kind, id), fields);
export const stopHeartbeat = (kind: HeartbeatKind, id: string | number) =>
  call<{ ok: boolean }>("DELETE", path(kind, id));

/** 270 → "4m30s", 3600 → "1h". */
export function formatDuration(seconds: number): string {
  let s = Math.max(0, Math.round(seconds));
  const parts: string[] = [];
  for (const [unit, size] of [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]] as const) {
    if (s >= size) {
      parts.push(`${Math.floor(s / size)}${unit}`);
      s %= size;
    }
  }
  return parts.join("") || "0s";
}

/** "270s", "60m", "1h", "4m30s" → seconds; null if it doesn't parse. */
export function parseDuration(text: string): number | null {
  const t = text.trim().toLowerCase();
  if (/^\d+$/.test(t)) return Number(t);
  const re = /(\d+)\s*([dhms])/g;
  let total = 0;
  let consumed = "";
  for (const m of t.matchAll(re)) {
    total += Number(m[1]) * { d: 86400, h: 3600, m: 60, s: 1 }[m[2] as "d" | "h" | "m" | "s"];
    consumed += m[0];
  }
  return consumed.replace(/\s/g, "") === t.replace(/\s/g, "") && total > 0 ? total : null;
}

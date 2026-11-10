/** A conversation's or task's heartbeat, as `/api/conversations` and
 *  `/api/tasks` include it (src/yuki_conductor/heartbeat.py). The agent sets
 *  and stops it from the conversation; the UI only shows it. */

export interface Heartbeat {
  target_kind: "conversation" | "task";
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

export const HEARTBEAT_ICON = "🕒";

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

/** One line for a tooltip: interval, state and the prompt. */
export function describeHeartbeat(hb: Heartbeat): string {
  const state = hb.paused ? "paused" : hb.ticking ? "checking now" : "active";
  return `Heartbeat every ${formatDuration(hb.interval_s)} (${state}): ${hb.prompt}`;
}

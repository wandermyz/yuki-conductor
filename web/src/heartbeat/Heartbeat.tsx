/**
 * Read-only heartbeat indicators. A heartbeat is set, changed and stopped by
 * asking the agent (`yuki-conductor heartbeat`), so the UI only shows that
 * one is active.
 */
import { useEffect, useState } from "react";
import { HEARTBEAT_ICON, describeHeartbeat, formatDuration } from "./api";
import type { Heartbeat } from "./api";
import "./heartbeat.css";

/** Re-render every `ms` so relative times stay current. */
function useNow(ms = 15000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** `🕒 4m30s · next in 2m · last: quiet`, for a thread or task header. */
export function HeartbeatChip({ hb }: { hb: Heartbeat }) {
  const now = useNow();
  let state: string;
  if (hb.ticking) state = "checking…";
  else if (hb.paused) state = "paused";
  else if (hb.next_due_at <= now) state = "due";
  else state = `next in ${formatDuration(Math.max(60, Math.round((hb.next_due_at - now) / 60) * 60))}`;
  const cls = [
    "heartbeat-chip",
    hb.paused && "paused",
    hb.ticking && "ticking",
    hb.last_outcome === "error" && "error",
  ].filter(Boolean).join(" ");
  return (
    <span className={cls} title={describeHeartbeat(hb)}>
      <span className="heartbeat-icon" aria-hidden="true">{HEARTBEAT_ICON}</span>
      <span className="heartbeat-chip-text">
        {formatDuration(hb.interval_s)}
        <span className="heartbeat-chip-sep">·</span>
        {state}
      </span>
      {hb.last_outcome && (
        <span className="heartbeat-chip-last">
          <span className="heartbeat-chip-sep">·</span>last: {hb.last_outcome}
        </span>
      )}
    </span>
  );
}

/** Just the icon, for list rows. */
export function HeartbeatMark({ hb }: { hb: Heartbeat }) {
  return (
    <span
      className={`heartbeat-mark${hb.paused ? " paused" : ""}${hb.ticking ? " ticking" : ""}`}
      title={describeHeartbeat(hb)}
      aria-label={describeHeartbeat(hb)}
    >
      {HEARTBEAT_ICON}
    </span>
  );
}

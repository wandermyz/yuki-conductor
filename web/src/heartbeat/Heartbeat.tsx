/**
 * Heartbeats: a recurring wake-up of a conversation's or task's own session
 * (src/yuki_conductor/heartbeat.py). The chip shows one at a glance; the
 * dialog sets, changes, pauses or stops it and lists its recent ticks.
 */
import { useEffect, useState } from "react";
import {
  DEFAULT_INTERVAL_S,
  MIN_INTERVAL_S,
  formatDuration,
  getHeartbeat,
  parseDuration,
  setHeartbeat,
  stopHeartbeat,
  updateHeartbeat,
} from "./api";
import type { Heartbeat, HeartbeatKind, HeartbeatTick } from "./api";
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

/** `♡ 4m30s · next in 2m · last: quiet` */
export function HeartbeatChip({ hb, onClick }: { hb: Heartbeat; onClick: () => void }) {
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
    <button
      className={cls}
      onClick={onClick}
      title={`Heartbeat: ${hb.prompt}`}
      aria-label={`Heartbeat every ${formatDuration(hb.interval_s)}, ${state}`}
    >
      <span className="heartbeat-icon" aria-hidden="true">♡</span>
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
    </button>
  );
}

function formatWhen(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** The interval field: free text ("60m", "1h"), validated as you type. */
export function IntervalInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (text: string) => void;
}) {
  const seconds = parseDuration(value);
  const invalid = seconds === null || seconds < MIN_INTERVAL_S;
  return (
    <span className="heartbeat-interval">
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={invalid}
        aria-label="Interval"
        placeholder="270s"
      />
      <small>
        {invalid
          ? `e.g. 270s, 60m, 1h (at least ${MIN_INTERVAL_S}s)`
          : seconds > 300 && seconds < 1800
            ? "Misses the 5-minute prompt cache on every tick"
            : `every ${formatDuration(seconds)}`}
      </small>
    </span>
  );
}

/**
 * Set, change, pause or stop a target's heartbeat, and read its tick log.
 * `onChanged` gets the new heartbeat (null once stopped).
 */
export function HeartbeatDialog({
  kind,
  targetId,
  onChanged,
  onClose,
}: {
  kind: HeartbeatKind;
  targetId: string | number;
  onChanged?: (hb: Heartbeat | null) => void;
  onClose: () => void;
}) {
  const [hb, setHb] = useState<Heartbeat | null | undefined>(undefined);
  const [ticks, setTicks] = useState<HeartbeatTick[]>([]);
  const [prompt, setPrompt] = useState("");
  const [interval, setIntervalText] = useState(`${DEFAULT_INTERVAL_S}s`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getHeartbeat(kind, targetId)
      .then((data) => {
        setHb(data.heartbeat);
        setTicks(data.ticks);
        if (data.heartbeat) {
          setPrompt(data.heartbeat.prompt);
          setIntervalText(formatDuration(data.heartbeat.interval_s));
        }
      })
      .catch((e) => setError(String(e)));
  }, [kind, targetId]);

  const run = async (fn: () => Promise<Heartbeat | null>, close = false) => {
    setBusy(true);
    setError(null);
    try {
      const next = await fn();
      setHb(next);
      onChanged?.(next);
      if (close) onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const seconds = parseDuration(interval);
  const valid = !!prompt.trim() && seconds !== null && seconds >= MIN_INTERVAL_S;
  const dirty = !hb || prompt.trim() !== hb.prompt || seconds !== hb.interval_s;

  const save = () =>
    run(async () => {
      if (!hb) return setHeartbeat(kind, targetId, prompt, seconds!);
      return updateHeartbeat(kind, targetId, {
        prompt: prompt.trim() !== hb.prompt ? prompt : undefined,
        interval_s: seconds !== hb.interval_s ? seconds! : undefined,
      });
    }, true);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content heartbeat-dialog" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">♡ Heartbeat</h3>
        <p className="heartbeat-help">
          Wakes this {kind}'s own session on an interval with the prompt below. Only replies
          marked for you are shown; quiet checks land in the log.
        </p>
        {hb === undefined && !error && <p className="placeholder">Loading…</p>}
        {hb !== undefined && (
          <>
            {hb && (
              <p className="heartbeat-summary">
                {hb.paused ? "Paused" : hb.ticking ? "Checking now" : `Next ${formatWhen(hb.next_due_at)}`}
                {" · "}set by {hb.created_by === "agent" ? "the agent" : "you"}
                {" · "}expires {formatWhen(hb.expires_at)}
              </p>
            )}
            <label className="heartbeat-field">
              <span>Every</span>
              <IntervalInput value={interval} onChange={setIntervalText} />
            </label>
            <label className="heartbeat-field heartbeat-field-prompt">
              <span>Prompt</span>
              <textarea
                rows={3}
                value={prompt}
                placeholder="What should each check look at, and what then?"
                onChange={(e) => setPrompt(e.target.value)}
              />
            </label>
            {error && <p className="task-error">{error}</p>}
            <div className="modal-actions heartbeat-actions">
              {hb && (
                <>
                  <button
                    className="heartbeat-stop"
                    disabled={busy}
                    onClick={() => run(async () => (await stopHeartbeat(kind, targetId), null), true)}
                  >
                    Stop
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => run(() => updateHeartbeat(kind, targetId, { paused: !hb.paused }))}
                  >
                    {hb.paused ? "Resume" : "Pause"}
                  </button>
                </>
              )}
              <span className="heartbeat-actions-spacer" />
              <button onClick={onClose}>Close</button>
              <button className="primary" disabled={busy || !valid || !dirty} onClick={save}>
                {hb ? "Save" : "Start"}
              </button>
            </div>
            {ticks.length > 0 && (
              <div className="heartbeat-log">
                <h4>Recent checks</h4>
                <ul>
                  {ticks.map((t) => (
                    <li key={t.id} className={`heartbeat-tick ${t.outcome ?? "running"}`}>
                      <span className="heartbeat-tick-when">{formatWhen(t.started_at)}</span>
                      <span className="heartbeat-tick-outcome">{t.outcome ?? "running"}</span>
                      <span className="heartbeat-tick-text">{t.error || t.response || ""}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

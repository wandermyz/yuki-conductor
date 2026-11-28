import { THRESHOLD } from "./usePullToRefresh";
import type { PullState } from "./usePullToRefresh";
import "./PullToRefresh.css";

/** The strip that opens above a list while it's pulled or refreshing. */
export default function PullIndicator({ pull, refreshing }: PullState) {
  const height = refreshing ? 44 : pull;
  const ready = pull >= THRESHOLD;
  return (
    <div
      className={`pull-indicator ${pull ? "pulling" : ""}`}
      style={{ height }}
      aria-live="polite"
      aria-hidden={!refreshing && !pull}
    >
      {refreshing ? (
        <span className="pull-spinner" aria-label="Refreshing" />
      ) : (
        pull > 0 && (
          <span className={`pull-arrow ${ready ? "ready" : ""}`} style={{ opacity: Math.min(pull / THRESHOLD, 1) }}>
            ↓
          </span>
        )
      )}
    </div>
  );
}

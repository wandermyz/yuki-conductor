/** Pieces the chat and the taskboard both draw. */
import type { StreamStep } from "./api";
import { HARNESS_ICONS } from "./harnessIcons";

export function HarnessIcon({ name }: { name: string | null }) {
  const src = name ? HARNESS_ICONS[name] : undefined;
  return src ? <img className="harness-icon" src={src} alt="" aria-hidden="true" /> : null;
}

const STEP_ICONS: Record<StreamStep["kind"], string> = {
  init: "●",
  text: "✎",
  thinking: "…",
  tool_use: "▸",
  tool_result: "↳",
};

/** How many trailing steps stay visible; older ones collapse away. */
const VISIBLE_STEPS = 8;

export function StepList({
  steps,
  running,
  agent,
}: {
  steps: StreamStep[];
  running: boolean;
  agent: string;
}) {
  const visible = steps.slice(-VISIBLE_STEPS);
  const hidden = steps.length - visible.length;
  return (
    <div className={running ? "step-list" : "step-list step-list-done"}>
      {hidden > 0 && (
        <div className="step-hidden">…{hidden} earlier step{hidden === 1 ? "" : "s"}</div>
      )}
      {visible.map((s) => (
        <div key={s.seq} className={`step step-${s.kind}`}>
          <span className="step-icon">{STEP_ICONS[s.kind] ?? "·"}</span>
          <span className="step-label">{s.label}</span>
        </div>
      ))}
      {running && <div className="responding-shimmer">{agent} is responding…</div>}
    </div>
  );
}


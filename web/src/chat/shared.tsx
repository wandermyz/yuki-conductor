/** Pieces the chat and the taskboard both draw. */
import { useState } from "react";
import type { HarnessInfo, Project, StreamStep } from "./api";
import { HARNESS_ICONS } from "./harnessIcons";

export function HarnessIcon({ name }: { name: string | null }) {
  const src = name ? HARNESS_ICONS[name] : undefined;
  return src ? <img className="harness-icon" src={src} alt="" aria-hidden="true" /> : null;
}

/**
 * The two-step "which agent, which project" modal that starts a conversation
 * or a task. The agent step is skipped when only one harness is available.
 */
export function NewSessionPicker({
  harnesses,
  projects,
  allowNoProject,
  onPick,
  onCancel,
}: {
  harnesses: HarnessInfo[];
  projects: Project[];
  /** Offer "No project" (the default working directory) as a choice. */
  allowNoProject: boolean;
  /** `harness` is null only when no harness list is known; the server picks its default. */
  onPick: (harness: string | null, project: string | null) => void;
  onCancel: () => void;
}) {
  const [chosen, setChosen] = useState<string | null>(null);
  // Derived rather than stored, so a harness list that arrives after the
  // picker opened still decides whether there is anything to ask.
  const single = harnesses.length <= 1;
  const harness = single ? (harnesses[0]?.name ?? null) : chosen;
  const label = harnesses.find((h) => h.name === harness)?.label ?? harness;

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-content project-picker" onClick={(e) => e.stopPropagation()}>
        {!single && chosen === null ? (
          <>
            <h3 className="modal-title">Select Agent</h3>
            <ul className="project-picker-list">
              {harnesses.map((h) => (
                <li key={h.name}>
                  <button className="project-picker-item harness-picker-item" onClick={() => setChosen(h.name)}>
                    <HarnessIcon name={h.name} />
                    <span className="harness-picker-text">
                      <span className="project-picker-name">{h.label}</span>
                      <span className="project-picker-path">{h.name}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="modal-actions">
              <button className="modal-close-btn" onClick={onCancel}>Cancel</button>
            </div>
          </>
        ) : (
          <>
            <h3 className="modal-title">
              Select Project
              {!single && (
                <span className="picker-step-context">
                  <HarnessIcon name={harness} />
                  {label}
                </span>
              )}
            </h3>
            <ul className="project-picker-list">
              {allowNoProject && (
                <li>
                  <button className="project-picker-item" onClick={() => onPick(harness, null)}>
                    <span className="project-picker-name">No project</span>
                    <span className="project-picker-path">Default working directory</span>
                  </button>
                </li>
              )}
              {projects.map((p) => (
                <li key={p.name}>
                  <button className="project-picker-item" onClick={() => onPick(harness, p.name)}>
                    <span className="project-picker-name">{p.name}</span>
                    <span className="project-picker-path">{p.path}</span>
                  </button>
                </li>
              ))}
              {!allowNoProject && projects.length === 0 && (
                <li className="placeholder">No projects — add one in Projects</li>
              )}
            </ul>
            <div className="modal-actions">
              {!single && (
                <button className="modal-close-btn" onClick={() => setChosen(null)}>Back</button>
              )}
              <button className="modal-close-btn" onClick={onCancel}>Cancel</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
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


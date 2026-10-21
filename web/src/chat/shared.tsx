/** Pieces the chat and the taskboard both draw. */
import { useState } from "react";
import { createPortal } from "react-dom";
import type { HarnessInfo, Project, StreamStep } from "./api";
import { HARNESS_ICONS } from "./harnessIcons";

export function HarnessIcon({ name }: { name: string | null }) {
  const src = name ? HARNESS_ICONS[name] : undefined;
  return src ? <img className="harness-icon" src={src} alt="" aria-hidden="true" /> : null;
}

/**
 * The two-step "which agent, which project" modal that starts a conversation
 * or a task. The agent step is skipped when only one harness is available.
 *
 * Rendered into `document.body`, so it looks the same wherever it's opened
 * rather than picking up the host view's styles (e.g. `.tasks-view button`).
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

  return createPortal(
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
    </div>,
    document.body,
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

/** A step's whole text: the label is a one-line cut of it. A tool result's
 *  label is all the backend keeps, so expanding it just unwraps the line. */
function fullText(step: StreamStep): string {
  const { name, summary, text } = step.detail;
  if ((step.kind === "text" || step.kind === "thinking") && typeof text === "string") return text;
  if (step.kind === "tool_use" && typeof name === "string" && typeof summary === "string") {
    return summary ? `${name}(${summary})` : name;
  }
  return step.label;
}

export function StepList({
  steps,
  running,
  agent,
}: {
  steps: StreamStep[];
  running: boolean;
  agent: string;
}) {
  const [showAll, setShowAll] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const visible = showAll ? steps : steps.slice(-VISIBLE_STEPS);
  const hidden = steps.length - visible.length;
  const toggle = (seq: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  return (
    <div className={running ? "step-list" : "step-list step-list-done"}>
      {hidden > 0 && (
        <div className="step-hidden" role="button" onClick={() => setShowAll(true)}>
          …{hidden} earlier step{hidden === 1 ? "" : "s"}
        </div>
      )}
      {visible.map((s) => {
        const open = expanded.has(s.seq);
        return (
          <div
            key={s.seq}
            className={`step step-${s.kind}${open ? " step-open" : ""}`}
            role="button"
            title={open ? "Collapse" : "Expand"}
            onClick={() => {
              // Don't collapse a step while the user is selecting text in it.
              if (!window.getSelection()?.isCollapsed) return;
              toggle(s.seq);
            }}
          >
            <span className="step-icon">{STEP_ICONS[s.kind] ?? "·"}</span>
            <span className="step-label">{open ? fullText(s) : s.label}</span>
          </div>
        );
      })}
      {running && <div className="responding-shimmer">{agent} is responding…</div>}
    </div>
  );
}

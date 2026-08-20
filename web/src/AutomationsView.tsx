import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  getAutomation,
  listAutomations,
  renameAutomation,
  runAutomation,
} from "./chat/api";
import type { Automation, CronRun } from "./chat/api";
import { copyText } from "./chat/Chat";

/** While a run is in flight, poll so the history fills in without a reload. */
const RUNNING_POLL_MS = 5000;

function formatTime(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDuration(run: CronRun): string {
  if (run.finished_at === null) return "in progress";
  const secs = Math.max(0, Math.round(run.finished_at - run.started_at));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  return `${mins}m ${secs % 60}s`;
}

/** The session id plus the exact command to reopen that run in Claude Code. */
function RunSession({ sessionId }: { sessionId: string }) {
  const [copied, setCopied] = useState<"id" | "cmd" | null>(null);

  const copy = async (what: "id" | "cmd", text: string) => {
    await copyText(text);
    setCopied(what);
    setTimeout(() => setCopied(null), 1500);
  };

  return (
    <div className="run-session">
      <span className="run-session-label">Session</span>
      <code className="run-session-id">{sessionId}</code>
      <button
        className="run-session-copy"
        title="Copy session id"
        onClick={() => copy("id", sessionId)}
      >
        {copied === "id" ? "copied" : "copy id"}
      </button>
      <button
        className="run-session-copy"
        title={`claude --resume ${sessionId}`}
        onClick={() => copy("cmd", `claude --resume ${sessionId}`)}
      >
        {copied === "cmd" ? "copied" : "copy --resume"}
      </button>
    </div>
  );
}

function RunRow({ run }: { run: CronRun }) {
  const [open, setOpen] = useState(false);
  const body = run.status === "error" ? run.error : run.response;

  return (
    <li className={`run-row run-${run.status}`}>
      <button className="run-summary" onClick={() => setOpen((v) => !v)}>
        <span className={`run-arrow ${open ? "open" : ""}`}>&#9656;</span>
        <span className={`run-status-dot ${run.status}`} />
        <span className="run-time">{formatTime(run.started_at)}</span>
        <span className="run-duration">{formatDuration(run)}</span>
        {run.trigger === "manual" && <span className="run-badge">manual</span>}
        {run.notified && <span className="run-badge notified">notified</span>}
      </button>
      {open && (
        <div className="run-body">
          {run.status === "running" ? (
            <p className="run-empty">Still running…</p>
          ) : body ? (
            run.status === "error" ? (
              <pre className="run-error">{body}</pre>
            ) : (
              <div className="run-response markdown">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
              </div>
            )
          ) : (
            <p className="run-empty">No output recorded.</p>
          )}
          {run.session_id && <RunSession sessionId={run.session_id} />}
          {run.conversation_id && (
            <a className="run-conv-link" href={`#chat/${run.conversation_id}`}>
              Open this run's conversation &rarr;
            </a>
          )}
        </div>
      )}
    </li>
  );
}

function AutomationDetail({
  automation,
  onRefresh,
  onRename,
  onBack,
}: {
  automation: Automation;
  onRefresh: () => void;
  onRename: (displayName: string) => void;
  onBack: () => void;
}) {
  const [promptOpen, setPromptOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const triggerRun = async () => {
    setError(null);
    setRunning(true);
    try {
      await runAutomation(automation.name);
      onRefresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  const inFlight = automation.running || running;
  const runs = automation.runs ?? [];

  return (
    <div className="automation-detail">
      <button className="back-btn" onClick={onBack}>
        &larr; Back
      </button>
      <header className="automation-detail-header">
        <EditableLabel value={automation.label} onSave={onRename} />
        <code className="automation-id">{automation.name}</code>
      </header>

      {automation.origin_conversation ? (
        <a
          className="automation-origin"
          href={`#chat/${automation.origin_conversation}`}
        >
          &#8617; Back to the conversation that set this up
        </a>
      ) : (
        <p className="automation-origin muted">
          No originating conversation recorded for this automation.
        </p>
      )}

      <section className="automation-section">
        <button
          className="automation-fold"
          onClick={() => setPromptOpen((v) => !v)}
        >
          <span className={`run-arrow ${promptOpen ? "open" : ""}`}>&#9656;</span>
          Prompt
        </button>
        {promptOpen && <pre className="automation-prompt">{automation.prompt}</pre>}

        <dl className="automation-cadence">
          <dt>Cron</dt>
          <dd>
            <code>{automation.schedule}</code>
          </dd>
          <dt>Cadence</dt>
          <dd>{automation.schedule_text}</dd>
          <dt>Next</dt>
          <dd>
            {automation.next_runs.length
              ? automation.next_runs.map(formatTime).join(" · ")
              : "—"}
          </dd>
        </dl>
      </section>

      <section className="automation-section">
        <div className="automation-runs-header">
          <h3>Recent runs</h3>
          <button
            className="new-chat-btn"
            onClick={triggerRun}
            disabled={inFlight}
          >
            {inFlight ? "Running…" : "Run now"}
          </button>
        </div>
        {error && <p className="automation-error">{error}</p>}
        {runs.length === 0 ? (
          <p className="run-empty">No runs recorded yet.</p>
        ) : (
          <ul className="run-list">
            {runs.map((r) => (
              <RunRow key={r.id} run={r} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function EditableLabel({
  value,
  onSave,
}: {
  value: string;
  onSave: (next: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(value);
    setEditing(false);
  }, [value]);

  useEffect(() => {
    if (editing) inputRef.current?.select();
  }, [editing]);

  const commit = () => {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== value) onSave(trimmed);
    setEditing(false);
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="title-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") {
            setDraft(value);
            setEditing(false);
          }
        }}
      />
    );
  }

  return (
    <div className="title-row">
      <h2>{value}</h2>
      <button
        className="edit-btn"
        onClick={() => setEditing(true)}
        title="Rename automation"
        aria-label="Rename automation"
      >
        &#9998;
      </button>
    </div>
  );
}

export default function AutomationsView() {
  const [automations, setAutomations] = useState<Automation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Automation | null>(null);
  const [loading, setLoading] = useState(true);

  const refreshList = useCallback(() => {
    return listAutomations()
      .then(setAutomations)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const refreshDetail = useCallback(() => {
    if (!selected) return;
    getAutomation(selected).then(setDetail).catch(console.error);
  }, [selected]);

  useEffect(() => {
    refreshList();
  }, [refreshList]);

  useEffect(() => {
    setDetail(null);
    refreshDetail();
  }, [refreshDetail]);

  // A manually triggered run finishes minutes later; poll until it settles so
  // its result lands in the history on its own.
  const anyRunning = detail?.runs?.some((r) => r.status === "running") ?? false;
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => {
      refreshDetail();
      refreshList();
    }, RUNNING_POLL_MS);
    return () => clearInterval(id);
  }, [anyRunning, refreshDetail, refreshList]);

  const handleRename = (displayName: string) => {
    if (!selected) return;
    renameAutomation(selected, displayName)
      .then((updated) => {
        setDetail((d) => (d ? { ...d, ...updated, runs: d.runs } : d));
        setAutomations((prev) =>
          prev.map((a) => (a.name === updated.name ? { ...a, ...updated } : a)),
        );
      })
      .catch(console.error);
  };

  return (
    <div className={`app automations-view ${selected ? "detail-open" : ""}`}>
      <aside className="sidebar">
        <div className="sidebar-header">
          <h2>Automations</h2>
        </div>
        {loading && <p className="placeholder">Loading…</p>}
        {!loading && automations.length === 0 && (
          <p className="placeholder">
            No automations defined. Ask in chat to schedule one.
          </p>
        )}
        <ul>
          {automations.map((a) => (
            <li
              key={a.name}
              className={a.name === selected ? "active" : ""}
              onClick={() => setSelected(a.name)}
            >
              <span className="session-title">
                {a.running && <span className="alive-dot alive" title="Running" />}
                {a.label}
              </span>
              <span className="session-date">{a.schedule_text}</span>
            </li>
          ))}
        </ul>
      </aside>
      <main className="content">
        {detail ? (
          <AutomationDetail
            automation={detail}
            onRefresh={refreshDetail}
            onRename={handleRename}
            onBack={() => setSelected(null)}
          />
        ) : (
          <p className="placeholder">Select an automation from the sidebar</p>
        )}
      </main>
    </div>
  );
}

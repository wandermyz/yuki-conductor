import { useEffect, useRef, useState } from "react";
import Chat from "./chat/Chat";
import Terminal from "./Terminal";
import "./App.css";

type Tab = "sessions" | "chat";

interface Session {
  thread_ts: string;
  session_id: string;
  channel_id: string | null;
  slack_url: string | null;
  title: string | null;
  session_type: "slack" | "zellij";
  project: string | null;
  alive?: boolean;
}

function formatTs(ts: string): string {
  const epoch = parseFloat(ts) * 1000;
  if (isNaN(epoch)) return ts;
  const d = new Date(epoch);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function EditableTitle({
  title,
  onSave,
}: {
  title: string;
  onSave: (newTitle: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setDraft(title);
    setEditing(false);
  }, [title]);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const save = () => {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== title) {
      onSave(trimmed);
    }
    setEditing(false);
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="title-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
          if (e.key === "Escape") {
            setDraft(title);
            setEditing(false);
          }
        }}
      />
    );
  }

  return (
    <div className="title-row">
      <h2>{title}</h2>
      <button
        className="edit-btn"
        onClick={() => setEditing(true)}
        title="Rename"
        aria-label="Rename"
      >
        &#9998;
      </button>
    </div>
  );
}

function groupByProject(sessions: Session[]): Map<string, Session[]> {
  const groups = new Map<string, Session[]>();
  for (const s of sessions) {
    const project = s.project || "Other";
    const list = groups.get(project) || [];
    list.push(s);
    groups.set(project, list);
  }
  return groups;
}

function NewZellijForm({
  project,
  onCreated,
}: {
  project: string;
  onCreated: (s: Session) => void;
}) {
  const [open, setOpen] = useState(false);
  const [worktree, setWorktree] = useState("");
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const submit = () => {
    const name = worktree.trim();
    if (!name) return;
    setLoading(true);
    fetch("/api/sessions/zellij", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, project, worktree: name }),
    })
      .then((r) => {
        if (!r.ok) throw new Error("Failed to create session");
        return r.json();
      })
      .then((data) => {
        onCreated(data as Session);
        setWorktree("");
        setOpen(false);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  if (!open) {
    return (
      <button
        className="new-session-btn"
        onClick={() => setOpen(true)}
        title="New Zellij session"
      >
        +
      </button>
    );
  }

  return (
    <span className="new-session-form">
      <input
        ref={inputRef}
        placeholder="worktree name"
        value={worktree}
        onChange={(e) => setWorktree(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          if (e.key === "Escape") {
            setWorktree("");
            setOpen(false);
          }
        }}
        disabled={loading}
      />
      <button onClick={submit} disabled={loading || !worktree.trim()}>
        Go
      </button>
    </span>
  );
}

function SessionsView() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [termKey, setTermKey] = useState(0);
  const [attached, setAttached] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [sidebarOpen, setSidebarOpen] = useState(true);

  useEffect(() => {
    fetch("/api/sessions")
      .then((r) => r.json())
      .then(setSessions)
      .catch(console.error);
  }, []);

  const active = sessions.find((s) => s.thread_ts === selected);

  const updateTitle = (threadTs: string, newTitle: string) => {
    fetch(`/api/sessions/${encodeURIComponent(threadTs)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: newTitle }),
    })
      .then((r) => {
        if (!r.ok) throw new Error("Failed to update title");
        setSessions((prev) =>
          prev.map((s) =>
            s.thread_ts === threadTs ? { ...s, title: newTitle } : s
          )
        );
      })
      .catch(console.error);
  };

  const addSession = (s: Session) => {
    setSessions((prev) => [{ ...s, alive: true }, ...prev]);
    setSelected(s.thread_ts);
    setAttached(false);
  };

  const closeZellijSession = (threadTs: string) => {
    fetch(`/api/sessions/zellij/${encodeURIComponent(threadTs)}`, {
      method: "DELETE",
    })
      .then((r) => {
        if (!r.ok) throw new Error("Failed to close session");
        setSessions((prev) =>
          prev.map((s) =>
            s.thread_ts === threadTs ? { ...s, alive: false } : s
          )
        );
        setAttached(false);
      })
      .catch(console.error);
  };

  const deleteZellijSession = (threadTs: string) => {
    // Kill if alive, then remove from DB
    const session = sessions.find((s) => s.thread_ts === threadTs);
    const doDelete = () => {
      fetch(`/api/sessions/${encodeURIComponent(threadTs)}`, {
        method: "DELETE",
      })
        .then((r) => {
          if (!r.ok) throw new Error("Failed to delete session");
          setSessions((prev) => prev.filter((s) => s.thread_ts !== threadTs));
          if (selected === threadTs) {
            setSelected(null);
            setAttached(false);
          }
        })
        .catch(console.error);
    };
    if (session?.alive) {
      fetch(`/api/sessions/zellij/${encodeURIComponent(threadTs)}`, {
        method: "DELETE",
      })
        .then((r) => {
          if (!r.ok) throw new Error("Failed to kill session");
          doDelete();
        })
        .catch(console.error);
    } else {
      doDelete();
    }
  };

  const reopenAndAttach = (threadTs: string) => {
    fetch(`/api/sessions/zellij/${encodeURIComponent(threadTs)}/reopen`, {
      method: "POST",
    })
      .then((r) => {
        if (!r.ok) throw new Error("Failed to reopen session");
        setSessions((prev) =>
          prev.map((s) =>
            s.thread_ts === threadTs ? { ...s, alive: true } : s
          )
        );
        setTermKey((k) => k + 1);
        setAttached(true);
      })
      .catch(console.error);
  };

  const attachToSession = () => {
    setTermKey((k) => k + 1);
    setAttached(true);
  };

  const grouped = groupByProject(sessions);

  const toggleGroup = (project: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(project)) next.delete(project);
      else next.add(project);
      return next;
    });
  };

  const renderZellijDetail = (s: Session) => {
    if (attached) {
      return (
        <div className="session-detail terminal-view">
          <div className="terminal-toolbar">
            <button className="back-btn" onClick={() => setAttached(false)}>
              &larr; Back
            </button>
            <EditableTitle
              title={s.title || "Untitled"}
              onSave={(t) => updateTitle(s.thread_ts, t)}
            />
            <button
              className="close-session-btn"
              onClick={() => closeZellijSession(s.thread_ts)}
              title="Kill Zellij session"
            >
              Close
            </button>
          </div>
          <Terminal key={termKey} sessionKey={s.thread_ts} />
        </div>
      );
    }

    return (
      <div className="session-detail">
        <button className="back-btn" onClick={() => { setSelected(null); setAttached(false); }}>
          &larr; Back
        </button>
        <EditableTitle
          title={s.title || "Untitled"}
          onSave={(t) => updateTitle(s.thread_ts, t)}
        />
        <div className="zellij-actions">
          <div className="zellij-status">
            <span className={`alive-dot ${s.alive ? "alive" : "dead"}`} />
            {s.alive ? "Running" : "Stopped"}
          </div>
          {s.alive ? (
            <button className="action-btn attach-btn" onClick={attachToSession}>
              Attach
            </button>
          ) : (
            <button className="action-btn resume-btn" onClick={() => reopenAndAttach(s.thread_ts)}>
              Resume
            </button>
          )}
          <button
            className="action-btn delete-btn"
            onClick={() => deleteZellijSession(s.thread_ts)}
          >
            Delete
          </button>
        </div>
      </div>
    );
  };

  return (
    <div className={`app ${selected ? "detail-open" : ""} ${sidebarOpen ? "" : "sidebar-collapsed"}`}>
      <aside className="sidebar">
        <div className="sidebar-header">
          <h2>{sidebarOpen ? "Sessions" : ""}</h2>
          <button
            className="sidebar-toggle"
            onClick={() => setSidebarOpen((v) => !v)}
            title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
          >
            {sidebarOpen ? "\u00AB" : "\u00BB"}
          </button>
        </div>
        {sidebarOpen && [...grouped.entries()].map(([project, items]) => (
          <div key={project} className="project-group">
            <div className="project-header">
              <button
                className="project-toggle"
                onClick={() => toggleGroup(project)}
              >
                <span className={`toggle-arrow ${collapsed.has(project) ? "collapsed" : ""}`}>
                  &#9662;
                </span>
                <span className="project-name">{project}</span>
              </button>
              <NewZellijForm project={project} onCreated={addSession} />
            </div>
            {!collapsed.has(project) && <ul>
              {items.map((s) => (
                <li
                  key={s.thread_ts}
                  className={s.thread_ts === selected ? "active" : ""}
                  onClick={() => {
                    setSelected(s.thread_ts);
                    setAttached(false);
                  }}
                >
                  <span className="session-title">
                    <span
                      className={`session-type-badge ${s.session_type}`}
                    >
                      {s.session_type === "slack" ? "S" : "Z"}
                    </span>
                    {s.session_type === "zellij" && (
                      <span
                        className={`alive-dot ${s.alive ? "alive" : "dead"}`}
                        title={s.alive ? "Running" : "Stopped"}
                      />
                    )}
                    {s.title || s.session_id.slice(0, 8)}
                  </span>
                  <span className="session-date">
                    {s.session_type === "slack" ? formatTs(s.thread_ts) : ""}
                  </span>
                </li>
              ))}
            </ul>}
          </div>
        ))}
      </aside>
      <main className="content">
        {active ? (
          active.session_type === "zellij" ? (
            renderZellijDetail(active)
          ) : (
            <div className="session-detail">
              <button className="back-btn" onClick={() => setSelected(null)}>
                &larr; Back
              </button>
              <EditableTitle
                title={active.title || "Untitled"}
                onSave={(t) => updateTitle(active.thread_ts, t)}
              />
              <dl>
                <dt>Date</dt>
                <dd>{formatTs(active.thread_ts)}</dd>
                <dt>Session ID</dt>
                <dd>{active.session_id}</dd>
                <dt>Channel ID</dt>
                <dd>{active.channel_id ?? "—"}</dd>
                <dt>Slack Thread</dt>
                <dd>
                  {active.slack_url ? (
                    <a href={active.slack_url} target="_blank" rel="noreferrer">
                      {active.slack_url}
                    </a>
                  ) : (
                    "No channel info"
                  )}
                </dd>
              </dl>
            </div>
          )
        ) : (
          <p className="placeholder">Select a conversation from the sidebar</p>
        )}
      </main>
    </div>
  );
}

function App() {
  const [tab, setTab] = useState<Tab>(() => {
    const saved = window.localStorage.getItem("yuki-tab");
    return saved === "chat" ? "chat" : "sessions";
  });

  useEffect(() => {
    window.localStorage.setItem("yuki-tab", tab);
  }, [tab]);

  return (
    <div className="root">
      <div className="tab-content">
        {tab === "chat" ? <Chat /> : <SessionsView />}
      </div>
      <nav className="tabbar" role="tablist">
        <button
          role="tab"
          aria-selected={tab === "chat"}
          className={`tab ${tab === "chat" ? "active" : ""}`}
          onClick={() => setTab("chat")}
        >
          <svg
            className="tab-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z" />
          </svg>
          <span className="tab-label">Chat</span>
        </button>
        <button
          role="tab"
          aria-selected={tab === "sessions"}
          className={`tab ${tab === "sessions" ? "active" : ""}`}
          onClick={() => setTab("sessions")}
        >
          <svg
            className="tab-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <path d="M3 9h18" />
            <path d="M8 4v5" />
          </svg>
          <span className="tab-label">Sessions</span>
        </button>
      </nav>
    </div>
  );
}

export default App;

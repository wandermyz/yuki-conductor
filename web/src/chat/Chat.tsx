import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  cancelProcessing,
  createConversation,
  deleteConversation,
  fetchStatuses,
  fetchProcessing,
  fetchAllUsage,
  listConversations,
  listMessages,
  listProjects,
  subscribeChatSocket,
  sendMessage,
  setConvStatus,
  uploadFile,
} from "./api";
import type {
  AttachmentRef,
  ChatMessage,
  Conversation,
  ConversationUsage,
  Project,
  WSEvent,
} from "./api";
import "./chat.css";

/* ---------- Notification sound (iOS-safe) ---------- */

let sharedAudioCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  try {
    if (!sharedAudioCtx || sharedAudioCtx.state === "closed") {
      sharedAudioCtx = new AudioContext();
    }
    return sharedAudioCtx;
  } catch {
    return null;
  }
}

/** Call from a user-gesture handler to unlock audio on iOS Safari. */
function unlockAudio() {
  const ctx = getAudioContext();
  if (ctx && ctx.state === "suspended") ctx.resume();
}

function playNotificationSound() {
  const ctx = getAudioContext();
  if (!ctx || ctx.state !== "running") return;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.frequency.value = 660;
  osc.type = "sine";
  gain.gain.setValueAtTime(0.3, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
  osc.start(ctx.currentTime);
  osc.stop(ctx.currentTime + 0.3);
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function isImage(mime: string | null, filename: string): boolean {
  if (mime?.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(filename);
}

/* ---------- Conversation status helpers ---------- */

type ConvStatus = "unread" | "read" | "done";

function formatTokens(n: number): string {
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(1) + "B";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

function formatUsage(u: ConversationUsage | undefined): string | null {
  if (!u) return null;
  if (u.cost_usd != null) return "$" + u.cost_usd.toFixed(2);
  if (u.total_tokens > 0) return formatTokens(u.total_tokens) + " tok";
  return null;
}

function getStatus(statuses: Record<string, ConvStatus>, id: string): ConvStatus {
  return statuses[id] ?? "read";
}

/* ---------- Session ID Modal ---------- */

function SessionIdModal({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(sessionId).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">Session ID</h3>
        <div className="session-id-display">
          <code className="session-id-code">{sessionId}</code>
          <button className="copy-btn" onClick={copy}>
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>
        <button className="modal-close-btn" onClick={onClose}>Close</button>
      </div>
    </div>
  );
}

/* ---------- Context Menu ---------- */

function ConvMenu({
  status,
  onShowSessionId,
  onSetStatus,
  onDelete,
  onClose,
}: {
  status: ConvStatus;
  onShowSessionId: () => void;
  onSetStatus: (s: ConvStatus) => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [onClose]);

  return (
    <div className="conv-menu" ref={ref}>
      <button
        className="conv-menu-item"
        onClick={(e) => { e.stopPropagation(); onShowSessionId(); onClose(); }}
      >
        Show Session ID
      </button>
      <div className="conv-menu-divider" />
      {status !== "read" && (
        <button
          className="conv-menu-item"
          onClick={(e) => { e.stopPropagation(); onSetStatus("read"); onClose(); }}
        >
          Mark as Read
        </button>
      )}
      {status !== "unread" && (
        <button
          className="conv-menu-item"
          onClick={(e) => { e.stopPropagation(); onSetStatus("unread"); onClose(); }}
        >
          Mark as Unread
        </button>
      )}
      {status !== "done" && (
        <button
          className="conv-menu-item"
          onClick={(e) => { e.stopPropagation(); onSetStatus("done"); onClose(); }}
        >
          Mark as Done
        </button>
      )}
      <div className="conv-menu-divider" />
      <button
        className="conv-menu-item conv-menu-item-danger"
        onClick={(e) => { e.stopPropagation(); onDelete(); onClose(); }}
      >
        Delete
      </button>
    </div>
  );
}

function MessageBubble({ msg, onRetry }: { msg: ChatMessage; onRetry: (msg: ChatMessage) => void }) {
  return (
    <div className={`bubble ${msg.role} ${msg.status ? `msg-${msg.status}` : ""}`}>
      {msg.text && (
        <div className="bubble-text markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ node: _node, ...props }) => (
                <a {...props} target="_blank" rel="noreferrer" />
              ),
            }}
          >
            {msg.text}
          </ReactMarkdown>
        </div>
      )}
      {msg.attachments.length > 0 && (
        <div className="bubble-attachments">
          {msg.attachments.map((a) =>
            isImage(a.mime_type, a.filename) ? (
              <a key={a.id} href={a.url} target="_blank" rel="noreferrer">
                <img src={a.url} alt={a.filename} />
              </a>
            ) : (
              <a key={a.id} className="file-link" href={a.url} download>
                📎 {a.filename}
              </a>
            ),
          )}
        </div>
      )}
      <div className="bubble-time">
        {formatTime(msg.created_at)}
        {msg.status === "sending" && <span className="msg-status"> · Sending…</span>}
        {msg.status === "failed" && (
          <span className="msg-status msg-status-failed">
            {" "}· Not sent{" "}
            <button
              type="button"
              className="msg-retry-btn"
              onClick={() => onRetry(msg)}
            >
              Retry
            </button>
          </span>
        )}
      </div>
    </div>
  );
}

function Composer({
  conversationId,
  onSend,
  onStop,
  disabled,
}: {
  conversationId: string;
  onSend: (text: string, files: AttachmentRef[]) => void;
  onStop: () => void;
  disabled: boolean;
}) {
  const draftKey = `draft:${conversationId}`;
  const [text, setText] = useState(() => localStorage.getItem(draftKey) ?? "");
  const [pending, setPending] = useState<AttachmentRef[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const adjustHeight = () => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  };

  useEffect(adjustHeight, [text]);

  useEffect(() => {
    if (text) {
      localStorage.setItem(draftKey, text);
    } else {
      localStorage.removeItem(draftKey);
    }
  }, [draftKey, text]);

  const submit = () => {
    if (!text.trim() && pending.length === 0) return;
    unlockAudio();
    onSend(text, pending);
    setText("");
    setPending([]);
    localStorage.removeItem(draftKey);
  };

  const onPickFiles = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const uploaded: AttachmentRef[] = [];
      for (const f of Array.from(files)) {
        uploaded.push(await uploadFile(f));
      }
      setPending((p) => [...p, ...uploaded]);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      {pending.length > 0 && (
        <div className="pending-attachments">
          {pending.map((a) => (
            <span key={a.id} className="pending-chip">
              📎 {a.filename}
              <button
                type="button"
                className="pending-remove"
                onClick={() =>
                  setPending((p) => p.filter((x) => x.id !== a.id))
                }
                aria-label="Remove attachment"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="composer-row">
        <button
          type="button"
          className="attach-btn"
          onClick={() => fileRef.current?.click()}
          disabled={uploading}
          title="Attach files"
          aria-label="Attach files"
        >
          +
        </button>
        <input
          ref={fileRef}
          type="file"
          multiple
          onChange={onPickFiles}
          style={{ display: "none" }}
        />
        <textarea
          ref={taRef}
          className="composer-input"
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (
              e.key === "Enter" &&
              !e.shiftKey &&
              !(e.nativeEvent as KeyboardEvent).isComposing &&
              !("ontouchstart" in window)
            ) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Message Claude…"
          disabled={disabled}
        />
        {disabled ? (
          <button
            type="button"
            className="stop-btn"
            onClick={onStop}
            aria-label="Stop"
          >
            ■
          </button>
        ) : (
          <button
            type="submit"
            className="send-btn"
            disabled={uploading || (!text.trim() && pending.length === 0)}
            aria-label="Send"
          >
            ↑
          </button>
        )}
      </div>
    </form>
  );
}

function ChatThread({
  conv,
  messages,
  processing,
  status,
  onBack,
  onSend,
  onRetry,
  onStop,
  onShowSessionId,
  onSetStatus,
  onDelete,
}: {
  conv: Conversation;
  messages: ChatMessage[];
  processing: boolean;
  status: ConvStatus;
  onBack: () => void;
  onSend: (text: string, files: AttachmentRef[]) => void;
  onRetry: (msg: ChatMessage) => void;
  onStop: () => void;
  onShowSessionId: () => void;
  onSetStatus: (s: ConvStatus) => void;
  onDelete: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);

  const isNearBottomRef = useRef(true);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const handleScroll = () => {
      isNearBottomRef.current =
        el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    };
    el.addEventListener("scroll", handleScroll);
    return () => el.removeEventListener("scroll", handleScroll);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && isNearBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, processing]);

  return (
    <div className="chat-thread">
      <header className="chat-thread-header">
        <button className="chat-back" onClick={onBack} aria-label="Back">
          ←
        </button>
        <h2 className="chat-thread-title">{conv.title || "New chat"}</h2>
        <div className="menu-anchor">
          <button
            className="chat-list-menu-btn"
            onClick={() => setHeaderMenuOpen(!headerMenuOpen)}
            aria-label="Menu"
            title="Menu"
          >
            &#8942;
          </button>
          {headerMenuOpen && (
            <ConvMenu
              status={status}
              onShowSessionId={onShowSessionId}
              onSetStatus={onSetStatus}
              onDelete={onDelete}
              onClose={() => setHeaderMenuOpen(false)}
            />
          )}
        </div>
      </header>
      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 && !processing && (
          <p className="chat-empty">Send a message to start the conversation.</p>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} msg={m} onRetry={onRetry} />
        ))}
        {processing && (
          <div className="responding-shimmer">Claude is responding…</div>
        )}
      </div>
      <Composer conversationId={conv.id} onSend={onSend} onStop={onStop} disabled={processing} />
    </div>
  );
}

export default function Chat({
  selectedId,
  onSelectId,
}: {
  selectedId: string | null;
  onSelectId: (id: string | null) => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const selected = selectedId;
  const setSelected = onSelectId;
  const [messagesByConv, setMessagesByConv] = useState<Record<string, ChatMessage[]>>({});
  const [processingConvs, setProcessingConvs] = useState<Set<string>>(new Set());
  const [statuses, setStatuses] = useState<Record<string, ConvStatus>>({});
  const [menuOpen, setMenuOpen] = useState<string | null>(null);
  const [sessionIdModal, setSessionIdModal] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [usage, setUsage] = useState<Record<string, ConversationUsage>>({});
  const [projects, setProjects] = useState<Project[]>([]);
  const [showProjectPicker, setShowProjectPicker] = useState(false);

  // Keep a ref to `selected` so the WS handler can read the latest value
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const setStatus = useCallback((id: string, s: ConvStatus) => {
    setStatuses((prev) => ({ ...prev, [id]: s }));
    setConvStatus(id, s).catch(console.error);
  }, []);

  // Subscribe to the shared WS connection (stays alive across tab switches)
  useEffect(() => {
    let firstOpen = true;
    const unsub = subscribeChatSocket({
      onEvent: (e: WSEvent) => {
        if (e.type === "reload") {
          window.location.reload();
          return;
        }
        const cid = e.conversation_id;
        if (e.type === "message") {
          setMessagesByConv((prev) => {
            const msgs = prev[cid] || [];
            if (msgs.find((m) => m.id === e.message.id)) return prev;
            // Reconcile any local optimistic placeholder for this same user
            // message (matched by text) so it isn't shown twice.
            const pruned =
              e.message.role === "user"
                ? msgs.filter(
                    (m) =>
                      !(m.id.startsWith("local-") && m.text === e.message.text),
                  )
                : msgs;
            return { ...prev, [cid]: [...pruned, e.message] };
          });
          // Mark as unread if this is an assistant message and the conversation is not currently selected
          if (e.message.role === "assistant" && selectedRef.current !== cid) {
            setStatuses((prev) => {
              if (prev[cid] === "done") return prev; // don't override "done"
              const next = { ...prev, [cid]: "unread" as ConvStatus };
              setConvStatus(cid, "unread").catch(console.error);
              return next;
            });
          }
        } else if (e.type === "processing") {
          if (!e.on) {
            playNotificationSound();
            // Refresh usage after an assistant response completes
            fetchAllUsage().then(setUsage).catch(console.error);
          }
          setProcessingConvs((prev) => {
            const next = new Set(prev);
            if (e.on) next.add(cid);
            else next.delete(cid);
            return next;
          });
        } else if (e.type === "title") {
          setConversations((prev) =>
            prev.map((c) => (c.id === cid ? { ...c, title: e.title } : c)),
          );
        }
      },
      onOpen: () => {
        setWsConnected(true);
        if (firstOpen) {
          firstOpen = false;
          return;
        }
        // Re-sync state after reconnect to pick up anything missed
        listConversations().then(setConversations).catch(console.error);
        fetchStatuses().then((s) => setStatuses(s as Record<string, ConvStatus>)).catch(console.error);
        fetchProcessing().then((p) => {
          setProcessingConvs(new Set(Object.keys(p)));
        }).catch(console.error);
        fetchAllUsage().then(setUsage).catch(console.error);
        const sel = selectedRef.current;
        if (sel) {
          listMessages(sel).then((msgs) => {
            setMessagesByConv((prev) => ({ ...prev, [sel]: msgs }));
          }).catch(console.error);
        }
      },
      onClose: () => setWsConnected(false),
    });
    return () => unsub();
  }, []);

  useEffect(() => {
    listConversations().then(setConversations).catch(console.error);
    fetchStatuses().then((s) => setStatuses(s as Record<string, ConvStatus>)).catch(console.error);
    fetchAllUsage().then(setUsage).catch(console.error);
    listProjects().then(setProjects).catch(console.error);
  }, []);

  // Load messages when selecting a conversation
  useEffect(() => {
    if (!selected) return;
    if (messagesByConv[selected]) return; // already loaded
    listMessages(selected).then((msgs) => {
      setMessagesByConv((prev) => ({ ...prev, [selected]: msgs }));
    });
  }, [selected, messagesByConv]);

  const newChat = async (projectName?: string) => {
    try {
      const c = await createConversation(undefined, projectName);
      setConversations((prev) => [c, ...prev]);
      setMessagesByConv((prev) => ({ ...prev, [c.id]: [] }));
      setSelected(c.id);
      setShowProjectPicker(false);
    } catch (e) {
      console.error(e);
    }
  };

  const handleNewChat = () => {
    if (projects.length === 0) {
      newChat();
    } else {
      setShowProjectPicker(true);
    }
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this conversation?")) return;
    try {
      await deleteConversation(id);
      setConversations((prev) => prev.filter((c) => c.id !== id));
      setMessagesByConv((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      if (selected === id) setSelected(null);
    } catch (e) {
      console.error(e);
    }
  };

  const handleSend = async (convId: string, text: string, files: AttachmentRef[]) => {
    // Optimistically render the message immediately so nothing typed is lost,
    // even if the network request fails. A temp id lets us update/dedupe later.
    const tempId = `local-${crypto.randomUUID()}`;
    const optimistic: ChatMessage = {
      id: tempId,
      conversation_id: convId,
      role: "user",
      text,
      attachments: files,
      created_at: Date.now() / 1000,
      status: "sending",
    };
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: [...(prev[convId] || []), optimistic],
    }));

    try {
      const userMsg = await sendMessage(
        convId,
        text,
        files.map((f) => f.id),
      );
      // Replace the optimistic placeholder with the server message. The WS
      // broadcast may also deliver it — dedupe by real id.
      setMessagesByConv((prev) => {
        const msgs = (prev[convId] || []).filter((m) => m.id !== tempId);
        if (msgs.find((m) => m.id === userMsg.id)) {
          return { ...prev, [convId]: msgs };
        }
        return { ...prev, [convId]: [...msgs, userMsg] };
      });
    } catch (e) {
      console.error(e);
      // Keep the message visible and flag it as not sent so it can be retried.
      setMessagesByConv((prev) => ({
        ...prev,
        [convId]: (prev[convId] || []).map((m) =>
          m.id === tempId ? { ...m, status: "failed" } : m,
        ),
      }));
    }
  };

  const handleRetry = (convId: string, msg: ChatMessage) => {
    // Drop the failed placeholder and re-send its contents.
    setMessagesByConv((prev) => ({
      ...prev,
      [convId]: (prev[convId] || []).filter((m) => m.id !== msg.id),
    }));
    handleSend(convId, msg.text, msg.attachments);
  };

  const selectConv = (id: string) => {
    setSelected(id);
    // Mark as read when selecting (unless it's done)
    setStatuses((prev) => {
      const cur = prev[id] ?? "read";
      if (cur === "unread") {
        setConvStatus(id, "read").catch(console.error);
        return { ...prev, [id]: "read" as ConvStatus };
      }
      return prev;
    });
  };

  const active = conversations.find((c) => c.id === selected);

  return (
    <div className={`chat ${selected ? "chat-detail-open" : ""}`}>
      {sessionIdModal && (
        <SessionIdModal sessionId={sessionIdModal} onClose={() => setSessionIdModal(null)} />
      )}
      {showProjectPicker && (
        <div className="modal-overlay" onClick={() => setShowProjectPicker(false)}>
          <div className="modal-content project-picker" onClick={(e) => e.stopPropagation()}>
            <h3 className="modal-title">Select Project</h3>
            <ul className="project-picker-list">
              <li>
                <button className="project-picker-item" onClick={() => newChat()}>
                  <span className="project-picker-name">No project</span>
                  <span className="project-picker-path">Default working directory</span>
                </button>
              </li>
              {projects.map((p) => (
                <li key={p.name}>
                  <button className="project-picker-item" onClick={() => newChat(p.name)}>
                    <span className="project-picker-name">{p.name}</span>
                    <span className="project-picker-path">{p.path}</span>
                  </button>
                </li>
              ))}
            </ul>
            <button className="modal-close-btn" onClick={() => setShowProjectPicker(false)}>Cancel</button>
          </div>
        </div>
      )}
      <aside className="chat-sidebar">
        <div className="chat-sidebar-header">
          <h2>Chats
            <span
              className={`ws-indicator ${wsConnected ? "ws-connected" : "ws-disconnected"}`}
              title={wsConnected ? "Connected" : "Disconnected"}
            />
          </h2>
          <button className="new-chat-btn" onClick={handleNewChat}>
            + New
          </button>
        </div>
        <div className="chat-filter-bar">
          <button
            className={`chat-filter-btn ${!showDone ? "active" : ""}`}
            onClick={() => setShowDone(false)}
          >
            Active
          </button>
          <button
            className={`chat-filter-btn ${showDone ? "active" : ""}`}
            onClick={() => setShowDone(true)}
          >
            All
          </button>
        </div>
        <ul className="chat-list">
          {conversations.length === 0 && (
            <li className="chat-empty-list">No conversations yet.</li>
          )}
          {conversations.filter((c) => showDone || getStatus(statuses, c.id) !== "done").map((c) => {
            const st = getStatus(statuses, c.id);
            return (
              <li
                key={c.id}
                className={c.id === selected ? "active" : ""}
                onClick={() => selectConv(c.id)}
              >
                <div className="chat-list-title-row">
                  {st === "unread" && <span className="status-dot" title="Unread" />}
                  {st === "done" && <span className="status-check" title="Done">&#10003;</span>}
                  <span className="chat-list-title">{c.title || "New chat"}</span>
                  {c.project && <span className="chat-list-project">{c.project}</span>}
                </div>
                <div className="chat-list-meta">
                  <span>{formatTime(c.updated_at)}</span>
                  {formatUsage(usage[c.id]) && (
                    <span className="chat-list-usage" title="Token usage / cost">
                      {formatUsage(usage[c.id])}
                    </span>
                  )}
                  <div className="chat-list-actions">
                    <div className="menu-anchor">
                      <button
                        className="chat-list-menu-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          setMenuOpen(menuOpen === c.id ? null : c.id);
                        }}
                        aria-label="Menu"
                        title="Menu"
                      >
                        &#8942;
                      </button>
                      {menuOpen === c.id && (
                        <ConvMenu
                          status={st}
                          onShowSessionId={() => setSessionIdModal(c.claude_session_id || "(no session yet)")}
                          onSetStatus={(s) => setStatus(c.id, s)}
                          onDelete={() => remove(c.id)}
                          onClose={() => setMenuOpen(null)}
                        />
                      )}
                    </div>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      </aside>
      <main className="chat-main">
        {active ? (
          <ChatThread
            key={active.id}
            conv={active}
            messages={messagesByConv[active.id] || []}
            processing={processingConvs.has(active.id)}
            status={getStatus(statuses, active.id)}
            onBack={() => setSelected(null)}
            onSend={(text, files) => handleSend(active.id, text, files)}
            onRetry={(msg) => handleRetry(active.id, msg)}
            onStop={() => cancelProcessing(active.id).catch(console.error)}
            onShowSessionId={() => setSessionIdModal(active.claude_session_id || "(no session yet)")}
            onSetStatus={(s) => setStatus(active.id, s)}
            onDelete={() => remove(active.id)}
          />
        ) : (
          <div className="chat-placeholder">
            <p>Pick a conversation, or start a new one.</p>
            <button className="new-chat-btn" onClick={handleNewChat}>
              + New Chat
            </button>
          </div>
        )}
      </main>
    </div>
  );
}

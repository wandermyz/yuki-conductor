import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  createConversation,
  deleteConversation,
  listConversations,
  listMessages,
  openChatSocket,
  sendMessage,
  uploadFile,
} from "./api";
import type {
  AttachmentRef,
  ChatMessage,
  Conversation,
  WSEvent,
} from "./api";
import "./chat.css";

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

function loadStatuses(): Record<string, ConvStatus> {
  try {
    return JSON.parse(localStorage.getItem("conv-statuses") ?? "{}");
  } catch {
    return {};
  }
}

function saveStatuses(s: Record<string, ConvStatus>) {
  localStorage.setItem("conv-statuses", JSON.stringify(s));
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
  onClose,
}: {
  status: ConvStatus;
  onShowSessionId: () => void;
  onSetStatus: (s: ConvStatus) => void;
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
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  return (
    <div className={`bubble ${msg.role}`}>
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
      <div className="bubble-time">{formatTime(msg.created_at)}</div>
    </div>
  );
}

function Composer({
  conversationId,
  onSend,
  disabled,
}: {
  conversationId: string;
  onSend: (text: string, files: AttachmentRef[]) => void;
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
          📎
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
            if (e.key === "Enter" && e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Message Claude…"
          disabled={disabled}
        />
        <button
          type="submit"
          className="send-btn"
          disabled={disabled || uploading || (!text.trim() && pending.length === 0)}
          aria-label="Send"
        >
          ↑
        </button>
      </div>
    </form>
  );
}

function ChatThread({
  conv,
  messages,
  processing,
  onBack,
  onSend,
}: {
  conv: Conversation;
  messages: ChatMessage[];
  processing: boolean;
  onBack: () => void;
  onSend: (text: string, files: AttachmentRef[]) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);

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
      </header>
      <div className="chat-scroll" ref={scrollRef}>
        {messages.length === 0 && !processing && (
          <p className="chat-empty">Send a message to start the conversation.</p>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} msg={m} />
        ))}
        {processing && (
          <div className="responding-shimmer">Claude is responding…</div>
        )}
      </div>
      <Composer conversationId={conv.id} onSend={onSend} disabled={processing} />
    </div>
  );
}

export default function Chat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [messagesByConv, setMessagesByConv] = useState<Record<string, ChatMessage[]>>({});
  const [processingConvs, setProcessingConvs] = useState<Set<string>>(new Set());
  const [statuses, setStatuses] = useState<Record<string, ConvStatus>>(loadStatuses);
  const [menuOpen, setMenuOpen] = useState<string | null>(null);
  const [sessionIdModal, setSessionIdModal] = useState<string | null>(null);

  // Keep a ref to `selected` so the WS handler can read the latest value
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const setStatus = useCallback((id: string, s: ConvStatus) => {
    setStatuses((prev) => {
      const next = { ...prev, [id]: s };
      saveStatuses(next);
      return next;
    });
  }, []);

  // Single global WS connection
  useEffect(() => {
    const ws = openChatSocket((e: WSEvent) => {
      const cid = e.conversation_id;
      if (e.type === "message") {
        setMessagesByConv((prev) => {
          const msgs = prev[cid] || [];
          if (msgs.find((m) => m.id === e.message.id)) return prev;
          return { ...prev, [cid]: [...msgs, e.message] };
        });
        // Mark as unread if this is an assistant message and the conversation is not currently selected
        if (e.message.role === "assistant" && selectedRef.current !== cid) {
          setStatuses((prev) => {
            if (prev[cid] === "done") return prev; // don't override "done"
            const next = { ...prev, [cid]: "unread" as ConvStatus };
            saveStatuses(next);
            return next;
          });
        }
      } else if (e.type === "processing") {
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
    });
    return () => ws.close();
  }, []);

  useEffect(() => {
    listConversations().then(setConversations).catch(console.error);
  }, []);

  // Load messages when selecting a conversation
  useEffect(() => {
    if (!selected) return;
    if (messagesByConv[selected]) return; // already loaded
    listMessages(selected).then((msgs) => {
      setMessagesByConv((prev) => ({ ...prev, [selected]: msgs }));
    });
  }, [selected, messagesByConv]);

  const newChat = async () => {
    try {
      const c = await createConversation();
      setConversations((prev) => [c, ...prev]);
      setMessagesByConv((prev) => ({ ...prev, [c.id]: [] }));
      setSelected(c.id);
    } catch (e) {
      console.error(e);
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
    try {
      const userMsg = await sendMessage(
        convId,
        text,
        files.map((f) => f.id),
      );
      // Optimistic render; WS will also broadcast — dedupe by id.
      setMessagesByConv((prev) => {
        const msgs = prev[convId] || [];
        if (msgs.find((m) => m.id === userMsg.id)) return prev;
        return { ...prev, [convId]: [...msgs, userMsg] };
      });
    } catch (e) {
      console.error(e);
    }
  };

  const selectConv = (id: string) => {
    setSelected(id);
    // Mark as read when selecting (unless it's done)
    setStatuses((prev) => {
      const cur = prev[id] ?? "read";
      if (cur === "unread") {
        const next = { ...prev, [id]: "read" as ConvStatus };
        saveStatuses(next);
        return next;
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
      <aside className="chat-sidebar">
        <div className="chat-sidebar-header">
          <h2>Chats</h2>
          <button className="new-chat-btn" onClick={newChat}>
            + New
          </button>
        </div>
        <ul className="chat-list">
          {conversations.length === 0 && (
            <li className="chat-empty-list">No conversations yet.</li>
          )}
          {conversations.map((c) => {
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
                </div>
                <div className="chat-list-meta">
                  <span>{formatTime(c.updated_at)}</span>
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
                          onClose={() => setMenuOpen(null)}
                        />
                      )}
                    </div>
                    <button
                      className="chat-list-delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        remove(c.id);
                      }}
                      aria-label="Delete"
                      title="Delete"
                    >
                      ×
                    </button>
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
            onBack={() => setSelected(null)}
            onSend={(text, files) => handleSend(active.id, text, files)}
          />
        ) : (
          <div className="chat-placeholder">
            <p>Pick a conversation, or start a new one.</p>
            <button className="new-chat-btn" onClick={newChat}>
              + New Chat
            </button>
          </div>
        )}
      </main>
    </div>
  );
}

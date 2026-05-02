import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  createConversation,
  deleteConversation,
  listConversations,
  listMessages,
  openConversationSocket,
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
  onSend,
  disabled,
}: {
  onSend: (text: string, files: AttachmentRef[]) => void;
  disabled: boolean;
}) {
  const [text, setText] = useState("");
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

  const submit = () => {
    if (!text.trim() && pending.length === 0) return;
    onSend(text, pending);
    setText("");
    setPending([]);
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
            if (e.key === "Enter" && !e.shiftKey) {
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
  onBack,
  onTitleChanged,
}: {
  conv: Conversation;
  onBack: () => void;
  onTitleChanged: (id: string, title: string) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [processing, setProcessing] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Keep onTitleChanged in a ref so the WS effect only depends on conv.id —
  // otherwise a re-render in App (e.g. title update) would tear down the socket.
  const titleChangedRef = useRef(onTitleChanged);
  useEffect(() => {
    titleChangedRef.current = onTitleChanged;
  }, [onTitleChanged]);

  useEffect(() => {
    let cancelled = false;
    listMessages(conv.id).then((m) => {
      if (!cancelled) setMessages(m);
    });
    const ws = openConversationSocket(conv.id, (e: WSEvent) => {
      if (e.type === "message") {
        setMessages((prev) =>
          prev.find((m) => m.id === e.message.id) ? prev : [...prev, e.message],
        );
      } else if (e.type === "processing") {
        setProcessing(e.on);
      } else if (e.type === "title") {
        titleChangedRef.current(conv.id, e.title);
      }
    });
    return () => {
      cancelled = true;
      ws.close();
    };
  }, [conv.id]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, processing]);

  const handleSend = async (text: string, files: AttachmentRef[]) => {
    try {
      const userMsg = await sendMessage(
        conv.id,
        text,
        files.map((f) => f.id),
      );
      // Optimistic render: WS will also broadcast this; dedupe by id.
      setMessages((prev) =>
        prev.find((m) => m.id === userMsg.id) ? prev : [...prev, userMsg],
      );
      // Belt-and-suspenders: poll briefly in case the WS missed the assistant
      // reply (e.g. a brand-new conversation where the WS handshake races the
      // first send). Stops as soon as a new assistant message arrives.
      const userMsgTime = userMsg.created_at;
      const stopAt = Date.now() + 120_000;
      const tick = async () => {
        if (Date.now() > stopAt) return;
        try {
          const fresh = await listMessages(conv.id);
          setMessages((prev) => {
            const existing = new Set(prev.map((m) => m.id));
            const merged = [...prev];
            for (const m of fresh) if (!existing.has(m.id)) merged.push(m);
            return merged;
          });
          if (fresh.some((m) => m.role === "assistant" && m.created_at > userMsgTime)) {
            return;
          }
        } catch {
          /* ignore */
        }
        setTimeout(tick, 2000);
      };
      setTimeout(tick, 2000);
    } catch (e) {
      console.error(e);
    }
  };

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
      <Composer onSend={handleSend} disabled={processing} />
    </div>
  );
}

export default function Chat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    listConversations().then(setConversations).catch(console.error);
  }, []);

  const newChat = async () => {
    try {
      const c = await createConversation();
      setConversations((prev) => [c, ...prev]);
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
      if (selected === id) setSelected(null);
    } catch (e) {
      console.error(e);
    }
  };

  const handleTitleChanged = (id: string, title: string) => {
    setConversations((prev) =>
      prev.map((c) => (c.id === id ? { ...c, title } : c)),
    );
  };

  const active = conversations.find((c) => c.id === selected);

  return (
    <div className={`chat ${selected ? "chat-detail-open" : ""}`}>
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
          {conversations.map((c) => (
            <li
              key={c.id}
              className={c.id === selected ? "active" : ""}
              onClick={() => setSelected(c.id)}
            >
              <div className="chat-list-title">{c.title || "New chat"}</div>
              <div className="chat-list-meta">
                <span>{formatTime(c.updated_at)}</span>
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
            </li>
          ))}
        </ul>
      </aside>
      <main className="chat-main">
        {active ? (
          <ChatThread
            key={active.id}
            conv={active}
            onBack={() => setSelected(null)}
            onTitleChanged={handleTitleChanged}
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

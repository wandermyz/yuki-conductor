import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  cancelProcessing,
  createConversation,
  archiveConversation,
  deleteConversation,
  fetchStatuses,
  fetchProcessing,
  fetchSteps,
  fetchAllUsage,
  listConversations,
  listHarnesses,
  listMessages,
  listProjects,
  searchConversations,
  subscribeChatSocket,
  sendMessage,
  setConvStatus,
  updateConversation,
  uploadFile,
} from "./api";
import type {
  AttachmentRef,
  ChatMessage,
  Conversation,
  ConversationUsage,
  ConvStatus,
  HarnessInfo,
  Project,
  SearchHit,
  StreamStep,
  WSEvent,
} from "./api";
import { pastedFiles } from "../paste";
import { disablePush, enablePush, getPushState } from "../webPush";
import type { PushState } from "../webPush";
import SidebarResizer from "../SidebarResizer";
import { useSidebarWidth } from "../sidebarWidth";
import "./chat.css";
import { HARNESS_ICONS } from "./harnessIcons";
import { HarnessIcon, NewSessionPicker, StepList } from "./shared";
/** Marks the agent a conversation runs on: its icon, or its name if it has none. */
function HarnessMarker({ name }: { name: string }) {
  if (!HARNESS_ICONS[name]) return <span className="chat-list-harness">{name}</span>;
  return (
    <span className="chat-harness-marker" title={name} aria-label={name}>
      <HarnessIcon name={name} />
    </span>
  );
}

/* ---------- Browser push notifications ---------- */

const PUSH_TITLES: Record<PushState, string> = {
  unsupported: "Notifications need HTTPS and a browser with Web Push",
  denied: "Notifications are blocked for this site in browser settings",
  off: "Turn on browser notifications for replies",
  on: "Browser notifications on — click to turn off",
};

/** Outlined bell (Lucide's `bell` / `bell-off`), matching the tab icons. */
function BellIcon({ off }: { off: boolean }) {
  return (
    <svg
      className="notify-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10.268 21a2 2 0 0 0 3.464 0" />
      {off ? (
        <>
          <path d="M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 12.499 6 8a6 6 0 0 1 .258-1.742" />
          <path d="m2 2 20 20" />
          <path d="M8.668 3.01A6 6 0 0 1 18 8c0 2.687.77 4.653 1.707 6.05" />
        </>
      ) : (
        <path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326" />
      )}
    </svg>
  );
}

function NotificationToggle() {
  const [state, setState] = useState<PushState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getPushState().then(setState).catch(console.error);
  }, []);

  if (state === null || state === "unsupported") return null;

  const toggle = async () => {
    setBusy(true);
    try {
      setState(await (state === "on" ? disablePush() : enablePush()));
    } catch (err) {
      console.error(err);
      alert(`Couldn't change notifications: ${err}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      className={`notify-toggle ${state === "on" ? "on" : ""}`}
      onClick={toggle}
      disabled={busy || state === "denied"}
      title={PUSH_TITLES[state]}
      aria-label={PUSH_TITLES[state]}
    >
      <BellIcon off={state !== "on"} />
    </button>
  );
}

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

function isVideo(mime: string | null, filename: string): boolean {
  if (mime?.startsWith("video/")) return true;
  return /\.(mp4|webm|mov|m4v)$/i.test(filename);
}

function isAudio(mime: string | null, filename: string): boolean {
  if (mime?.startsWith("audio/")) return true;
  return /\.(mp3|wav|ogg|oga|opus|flac|m4a|aac)$/i.test(filename);
}

/* ---------- Conversation status helpers ---------- */

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

/* ---------- Context window occupation ---------- */

/** One turn's context: prompt tokens carried, out of the model's window. */
interface ContextUse {
  tokens: number;
  window: number;
}

function contextOf(msg: ChatMessage): ContextUse | null {
  if (msg.role !== "assistant") return null;
  const tokens = msg.context_tokens;
  const window = msg.context_window;
  if (tokens == null || !window) return null;
  return { tokens, window };
}

/** The newest turn that reported one: what the conversation is carrying now. */
function latestContext(messages: ChatMessage[]): ContextUse | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const use = contextOf(messages[i]);
    if (use) return use;
  }
  return null;
}

function contextPct(use: ContextUse): number {
  return Math.min(100, Math.round((use.tokens / use.window) * 100));
}

function contextTitle(use: ContextUse): string {
  return (
    `Context window: ${use.tokens.toLocaleString()} of ${use.window.toLocaleString()} ` +
    `tokens used (${contextPct(use)}%)`
  );
}

/** Colour says what the number is for: room left, nearly full, or full. */
function contextLevel(pct: number): string {
  return pct >= 85 ? "high" : pct >= 60 ? "mid" : "low";
}

function ContextMeter({ use }: { use: ContextUse }) {
  const pct = contextPct(use);
  return (
    <span
      className={`ctx-meter ctx-${contextLevel(pct)}`}
      title={contextTitle(use)}
    >
      <span className="ctx-bar" aria-hidden="true">
        <span className="ctx-fill" style={{ width: `${Math.max(3, pct)}%` }} />
      </span>
      <span className="ctx-text">
        {formatTokens(use.tokens)} / {formatTokens(use.window)}
        <span className="ctx-pct">{pct}%</span>
      </span>
    </span>
  );
}

function getStatus(statuses: Record<string, ConvStatus>, id: string): ConvStatus {
  return statuses[id] ?? "read";
}

/* ---------- Insecure-context fallbacks ----------
 *
 * The web server binds 0.0.0.0 and is normally reached over plain HTTP at a
 * Tailscale address, which is not a secure context. Secure-context-only APIs
 * are undefined there rather than merely failing, so calling one throws and
 * takes the whole handler with it. Only https:// and http://localhost get the
 * real implementations.
 */

/** Local-only id for optimistic rendering: a dedupe key, not a security token. */
function newTempId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `local-${crypto.randomUUID()}`;
  }
  return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Copy to clipboard, falling back to a hidden textarea off secure contexts. */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    if (!document.execCommand("copy")) {
      throw new Error("copy command was rejected");
    }
  } finally {
    document.body.removeChild(ta);
  }
}

/* ---------- Session ID Modal ---------- */

export function SessionIdModal({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    copyText(sessionId)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch((e) => console.error("copy failed", e));
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

/* ---------- Rename Modal ---------- */

function RenameModal({
  initialTitle,
  onSubmit,
  onClose,
}: {
  initialTitle: string;
  onSubmit: (title: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState(initialTitle);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
    onClose();
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">Rename Conversation</h3>
        <input
          ref={inputRef}
          className="rename-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") onClose();
          }}
          placeholder="Conversation title"
        />
        <div className="rename-actions">
          <button className="modal-close-btn" onClick={onClose}>Cancel</button>
          <button className="copy-btn" onClick={submit} disabled={!value.trim()}>
            Rename
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---------- Context Menu ---------- */

function ConvMenu({
  status,
  onShowSessionId,
  onRename,
  onSetStatus,
  onArchive,
  onDelete,
  onClose,
}: {
  status: ConvStatus;
  onShowSessionId: () => void;
  onRename: () => void;
  onSetStatus: (s: ConvStatus) => void;
  onArchive: () => void;
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
        onClick={(e) => { e.stopPropagation(); onRename(); onClose(); }}
      >
        Rename
      </button>
      <button
        className="conv-menu-item"
        onClick={(e) => { e.stopPropagation(); onShowSessionId(); onClose(); }}
      >
        Show Session ID
      </button>
      <div className="conv-menu-divider" />
      {/* Status moves one step at a time: unread <-> read <-> done. */}
      {status === "unread" && (
        <button
          className="conv-menu-item"
          onClick={(e) => { e.stopPropagation(); onSetStatus("read"); onClose(); }}
        >
          Mark as Read
        </button>
      )}
      {status === "read" && (
        <>
          <button
            className="conv-menu-item"
            onClick={(e) => { e.stopPropagation(); onSetStatus("unread"); onClose(); }}
          >
            Mark as Unread
          </button>
          <button
            className="conv-menu-item"
            onClick={(e) => { e.stopPropagation(); onSetStatus("done"); onClose(); }}
          >
            Mark as Done
          </button>
        </>
      )}
      {status === "done" && (
        <button
          className="conv-menu-item"
          onClick={(e) => { e.stopPropagation(); onSetStatus("read"); onClose(); }}
        >
          Mark as Not Done
        </button>
      )}
      <div className="conv-menu-divider" />
      <button
        className="conv-menu-item"
        onClick={(e) => { e.stopPropagation(); onArchive(); onClose(); }}
      >
        Archive
      </button>
      <button
        className="conv-menu-item conv-menu-item-danger"
        onClick={(e) => { e.stopPropagation(); onDelete(); onClose(); }}
      >
        Delete
      </button>
    </div>
  );
}

/* ---------- Message actions (copy / read aloud) ---------- */

/**
 * Flatten markdown into something a speech synthesizer reads sensibly:
 * drop fenced code, list bullets, emphasis/heading markers, and link syntax.
 */
function markdownToSpeech(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " code block. ")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+\.\s+/gm, "")
    .replace(/(\*\*|__|\*|_|~~)/g, "")
    .replace(/^\s*\|.*\|\s*$/gm, (row) =>
      row.replace(/\|/g, " ").replace(/^[\s-:]+$/, ""),
    )
    .replace(/\n{2,}/g, ". ")
    .replace(/\s+/g, " ")
    .trim();
}

function MessageActions({
  text,
  canSpeak,
  readerUrl,
}: {
  text: string;
  canSpeak: boolean;
  readerUrl?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;

  // A live utterance outlives this component's render; stop it on unmount.
  useEffect(() => {
    return () => {
      if (speaking) synth?.cancel();
    };
  }, [speaking, synth]);

  const onCopy = async () => {
    try {
      await copyText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable — leave the icon unchanged */
    }
  };

  // Must run inside the click handler: iOS Safari only unlocks speech
  // synthesis from a direct user gesture.
  const onSpeak = () => {
    if (!synth) return;
    if (speaking) {
      synth.cancel();
      setSpeaking(false);
      return;
    }
    const spoken = markdownToSpeech(text);
    if (!spoken) return;
    synth.cancel();
    const utter = new SpeechSynthesisUtterance(spoken);
    utter.lang = document.documentElement.lang || navigator.language || "en-US";
    utter.onend = () => setSpeaking(false);
    utter.onerror = () => setSpeaking(false);
    setSpeaking(true);
    synth.speak(utter);
  };

  return (
    <div className="bubble-actions">
      <button
        type="button"
        className="bubble-action"
        onClick={onCopy}
        title="Copy markdown"
        aria-label="Copy message as markdown"
      >
        {copied ? "✓" : "⧉"}
      </button>
      {synth && canSpeak && (
        <button
          type="button"
          className={`bubble-action ${speaking ? "is-active" : ""}`}
          onClick={onSpeak}
          title={speaking ? "Stop reading" : "Read aloud"}
          aria-label={speaking ? "Stop reading message" : "Read message aloud"}
        >
          {speaking ? "■" : "▶"}
        </button>
      )}
      {readerUrl && (
        <a
          className="bubble-action"
          href={readerUrl}
          target="_blank"
          rel="noreferrer"
          title="Open as article (for Safari Reader)"
          aria-label="Open message as an article page"
        >
          ¶
        </a>
      )}
    </div>
  );
}

function MessageBubble({
  msg,
  highlighted,
  onRetry,
}: {
  msg: ChatMessage;
  highlighted?: boolean;
  onRetry: (msg: ChatMessage) => void;
}) {
  const use = contextOf(msg);
  return (
    <div
      id={`msg-${msg.id}`}
      className={`bubble ${msg.role} ${msg.status ? `msg-${msg.status}` : ""} ${highlighted ? "bubble-highlight" : ""}`}
    >
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
            ) : isVideo(a.mime_type, a.filename) ? (
              <video key={a.id} src={a.url} controls loop playsInline preload="metadata" />
            ) : isAudio(a.mime_type, a.filename) ? (
              <div key={a.id} className="audio-attachment">
                <audio src={a.url} controls preload="metadata" />
                <a className="file-link" href={a.url} download>
                  📎 {a.filename}
                </a>
              </div>
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
        {use && (
          <span
            className={`bubble-ctx ctx-${contextLevel(contextPct(use))}`}
            title={contextTitle(use)}
          >
            {formatTokens(use.tokens)}/{formatTokens(use.window)} ({contextPct(use)}%)
          </span>
        )}
        {msg.text && (
          <MessageActions
            text={msg.text}
            canSpeak={msg.role === "assistant"}
            readerUrl={
              msg.role === "assistant"
                ? `/read/${encodeURIComponent(msg.conversation_id)}/${encodeURIComponent(msg.id)}`
                : undefined
            }
          />
        )}
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

/* ---------- Live step stream ---------- */

/** What the UI calls the agent behind a conversation. */
function agentName(conv: Conversation): string {
  return conv.harness === "claude-code" ? "Claude" : conv.harness;
}

function Composer({
  conversationId,
  agent,
  onSend,
  onStop,
  disabled,
}: {
  conversationId: string;
  agent: string;
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

  const addFiles = async (files: File[]) => {
    if (files.length === 0) return;
    setUploading(true);
    try {
      const uploaded: AttachmentRef[] = [];
      for (const f of files) {
        uploaded.push(await uploadFile(f));
      }
      setPending((p) => [...p, ...uploaded]);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const onPickFiles = (e: React.ChangeEvent<HTMLInputElement>) =>
    addFiles(Array.from(e.target.files ?? []));

  const onPaste = (e: React.ClipboardEvent) => {
    const files = pastedFiles(e);
    if (!files.length) return;
    e.preventDefault();
    addFiles(files);
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
          onPaste={onPaste}
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
          placeholder={`Message ${agent}…`}
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

/* ---------- Search ---------- */

/** A message to scroll to and flash, from a search hit. `n` makes each click
 *  a new request, so clicking the same hit twice scrolls again. */
interface Highlight {
  messageId: string;
  n: number;
}

/** Render a search snippet, turning its \x02…\x03 markers into <mark>. */
function Snippet({ text }: { text: string }) {
  // "a\x02b\x03c" splits on \x02 into ["a", "b\x03c"]: every piece after the
  // first opens with a matched term, ending at its \x03.
  const [head, ...rest] = text.split("\x02");
  return (
    <>
      {head}
      {rest.map((p, i) => {
        const [term, tail] = p.split("\x03");
        return (
          <span key={i}>
            <mark>{term}</mark>
            {tail}
          </span>
        );
      })}
    </>
  );
}

function SearchResults({
  hits,
  loading,
  selected,
  onPick,
}: {
  hits: SearchHit[] | null;
  loading: boolean;
  selected: string | null;
  onPick: (hit: SearchHit) => void;
}) {
  return (
    <ul className="chat-list chat-search-results">
      {hits === null && loading && <li className="chat-empty-list">Searching…</li>}
      {hits?.length === 0 && <li className="chat-empty-list">No matches.</li>}
      {hits?.map((h) => (
        <li
          key={h.conversation_id}
          className={h.conversation_id === selected ? "active" : ""}
          onClick={() => onPick(h)}
        >
          <div className="chat-list-title-row">
            <span className="chat-list-title">{h.title || "New chat"}</span>
          </div>
          <div className="chat-search-snippet">
            <Snippet text={h.snippet} />
          </div>
        </li>
      ))}
    </ul>
  );
}

function ChatThread({
  conv,
  messages,
  processing,
  steps,
  status,
  highlight,
  onBack,
  onSend,
  onRetry,
  onStop,
  onShowSessionId,
  onRename,
  onSetStatus,
  onArchive,
  onDelete,
}: {
  conv: Conversation;
  messages: ChatMessage[];
  processing: boolean;
  steps: StreamStep[];
  status: ConvStatus;
  highlight: Highlight | null;
  onBack: () => void;
  onSend: (text: string, files: AttachmentRef[]) => void;
  onRetry: (msg: ChatMessage) => void;
  onStop: () => void;
  onShowSessionId: () => void;
  onRename: () => void;
  onSetStatus: (s: ConvStatus) => void;
  onArchive: () => void;
  onDelete: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);
  const contextUse = latestContext(messages);

  // Steps belong to the turn that produced the last assistant message, so once
  // the run finishes that message renders below the trace rather than above it.
  const last = messages[messages.length - 1];
  const finalMessage =
    !processing && steps.length > 0 && last?.role === "assistant" ? last : null;
  const headMessages = finalMessage ? messages.slice(0, -1) : messages;

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
  }, [messages, processing, steps]);

  // Scroll a search hit into view once its message has loaded. Runs after the
  // stick-to-bottom effect above, so it wins on the first load. A hit older
  // than the loaded page of messages isn't found and the thread stays put.
  const scrolledFor = useRef<number | null>(null);
  useEffect(() => {
    if (!highlight || scrolledFor.current === highlight.n) return;
    const target = document.getElementById(`msg-${highlight.messageId}`);
    if (!target) return;
    scrolledFor.current = highlight.n;
    isNearBottomRef.current = false;
    target.scrollIntoView({ block: "center" });
  }, [highlight, messages]);

  return (
    <div className="chat-thread">
      <header className="chat-thread-header">
        <button className="chat-back" onClick={onBack} aria-label="Back">
          ←
        </button>
        <h2 className="chat-thread-title">{conv.title || "New chat"}</h2>
        <HarnessMarker name={conv.harness} />
        {contextUse && <ContextMeter use={contextUse} />}
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
              onRename={onRename}
              onSetStatus={onSetStatus}
              onArchive={onArchive}
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
        {headMessages.map((m) => (
          <MessageBubble
            key={m.id}
            msg={m}
            highlighted={highlight?.messageId === m.id}
            onRetry={onRetry}
          />
        ))}
        {(processing || steps.length > 0) && (
          <StepList steps={steps} running={processing} agent={agentName(conv)} />
        )}
        {finalMessage && (
          <MessageBubble
            key={finalMessage.id}
            msg={finalMessage}
            highlighted={highlight?.messageId === finalMessage.id}
            onRetry={onRetry}
          />
        )}
      </div>
      <Composer
        conversationId={conv.id}
        agent={agentName(conv)}
        onSend={onSend}
        onStop={onStop}
        disabled={processing}
      />
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
  const [stepsByConv, setStepsByConv] = useState<Record<string, StreamStep[]>>({});
  const [statuses, setStatuses] = useState<Record<string, ConvStatus>>({});
  const [menuOpen, setMenuOpen] = useState<string | null>(null);
  const [sessionIdModal, setSessionIdModal] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const sidebar = useSidebarWidth("chat-sidebar-width", 280);
  const [usage, setUsage] = useState<Record<string, ConversationUsage>>({});
  const [projects, setProjects] = useState<Project[]>([]);
  // New-conversation picker: an agent (when there's a choice), then a project.
  const [picking, setPicking] = useState(false);
  // Harnesses a new conversation can use.
  const [harnesses, setHarnesses] = useState<HarnessInfo[]>([]);
  // Sidebar search: while the box has text, results replace the chat list.
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [highlight, setHighlight] = useState<Highlight | null>(null);

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
        if (e.type === "task") return; // the Tasks tab's business
        const cid = e.conversation_id;
        // Taskboard turns stream on the same socket, keyed `task:<id>`.
        if (cid.startsWith("task:")) return;
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
          // An out-of-band push has no `processing` event to close the turn,
          // so alert here instead — otherwise it arrives silently.
          if (e.pushed) {
            playNotificationSound();
          }
          // An out-of-band push can be the first we hear of a conversation
          // (a cron run opening its own thread). Without this it stays absent
          // from the sidebar until a reload.
          setConversations((prev) => {
            if (prev.some((c) => c.id === cid)) return prev;
            listConversations().then(setConversations).catch(console.error);
            return prev;
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
        } else if (e.type === "status") {
          // Server-driven status change (e.g. an out-of-band push marking the
          // conversation unread). Trust it only while the user isn't looking at
          // the thread — otherwise it would un-read what they're reading.
          if (selectedRef.current !== cid) {
            setStatuses((prev) => ({ ...prev, [cid]: e.status }));
          }
        } else if (e.type === "step") {
          // Steps are ephemeral progress for the in-flight turn. Dedupe by seq
          // so a reconnect replay can't double-render what's already shown.
          setStepsByConv((prev) => {
            const cur = prev[cid] || [];
            if (cur.some((s) => s.seq === e.step.seq)) return prev;
            return { ...prev, [cid]: [...cur, e.step] };
          });
        } else if (e.type === "processing") {
          if (!e.on) {
            playNotificationSound();
            // Refresh usage after an assistant response completes
            fetchAllUsage().then(setUsage).catch(console.error);
          }
          setStepsByConv((prev) => {
            // A new turn starts fresh; a finished turn keeps its steps on
            // screen alongside the final message.
            if (!e.on) return prev;
            return { ...prev, [cid]: [] };
          });
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
        // Resume the step stream: replace buffers wholesale with the server's
        // snapshot, so a run that started while we were disconnected still
        // shows its progress instead of an empty list.
        fetchSteps().then(setStepsByConv).catch(console.error);
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
    listHarnesses()
      .then((h) => setHarnesses(h.harnesses.filter((x) => x.available)))
      .catch(console.error);
    // A run may already be in flight from before this page loaded.
    fetchProcessing().then((p) => setProcessingConvs(new Set(Object.keys(p)))).catch(console.error);
    fetchSteps().then(setStepsByConv).catch(console.error);
  }, []);

  // Debounced search; a stale response never overwrites a newer query's.
  useEffect(() => {
    const q = query.trim();
    if (!q) {
      setHits(null);
      setSearching(false);
      return;
    }
    let cancelled = false;
    setSearching(true);
    const timer = setTimeout(() => {
      searchConversations(q)
        .then((h) => {
          if (!cancelled) setHits(h);
        })
        .catch(console.error)
        .finally(() => {
          if (!cancelled) setSearching(false);
        });
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query]);

  // Load messages when selecting a conversation
  useEffect(() => {
    if (!selected) return;
    if (messagesByConv[selected]) return; // already loaded
    listMessages(selected).then((msgs) => {
      setMessagesByConv((prev) => ({ ...prev, [selected]: msgs }));
    });
  }, [selected, messagesByConv]);

  const newChat = async (harness: string | null, projectName: string | null) => {
    try {
      const c = await createConversation(undefined, projectName ?? undefined, harness ?? undefined);
      setConversations((prev) => [c, ...prev]);
      setMessagesByConv((prev) => ({ ...prev, [c.id]: [] }));
      setSelected(c.id);
      setPicking(false);
    } catch (e) {
      console.error(e);
    }
  };

  const handleNewChat = () => setPicking(true);

  const dropFromView = (id: string) => {
    setConversations((prev) => prev.filter((c) => c.id !== id));
    setMessagesByConv((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    if (selected === id) setSelected(null);
  };

  const remove = async (id: string) => {
    if (!confirm("Delete this conversation?")) return;
    try {
      await deleteConversation(id);
      dropFromView(id);
    } catch (e) {
      console.error(e);
    }
  };

  // Archived conversations never come back in the UI; they stay in the DB.
  const archive = async (id: string) => {
    if (!confirm("Archive this conversation? It will disappear from the web UI.")) return;
    try {
      await archiveConversation(id);
      dropFromView(id);
    } catch (e) {
      console.error(e);
    }
  };

  const rename = async (id: string, title: string) => {
    // Optimistic: the sidebar and header relabel immediately; revert on failure.
    const prevTitle = conversations.find((c) => c.id === id)?.title ?? null;
    setConversations((prev) =>
      prev.map((c) => (c.id === id ? { ...c, title } : c)),
    );
    try {
      await updateConversation(id, { title });
    } catch (e) {
      console.error(e);
      setConversations((prev) =>
        prev.map((c) => (c.id === id ? { ...c, title: prevTitle } : c)),
      );
    }
  };

  const handleSend = async (convId: string, text: string, files: AttachmentRef[]) => {
    // Optimistically render the message immediately so nothing typed is lost,
    // even if the network request fails. A temp id lets us update/dedupe later.
    const tempId = newTempId();
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

  const openHit = (hit: SearchHit) => {
    setHighlight((prev) => ({ messageId: hit.message_id, n: (prev?.n ?? 0) + 1 }));
    selectConv(hit.conversation_id);
  };

  const active = conversations.find((c) => c.id === selected);

  return (
    <div
      className={`chat ${selected ? "chat-detail-open" : ""}`}
      style={sidebar.style}
    >
      {sessionIdModal && (
        <SessionIdModal sessionId={sessionIdModal} onClose={() => setSessionIdModal(null)} />
      )}
      {renaming && (
        <RenameModal
          initialTitle={conversations.find((c) => c.id === renaming)?.title || ""}
          onSubmit={(title) => rename(renaming, title)}
          onClose={() => setRenaming(null)}
        />
      )}
      {picking && (
        <NewSessionPicker
          harnesses={harnesses}
          projects={projects}
          allowNoProject
          onPick={newChat}
          onCancel={() => setPicking(false)}
        />
      )}
      <aside className="chat-sidebar">
        <div className="chat-sidebar-header">
          <h2>Chats
            <span
              className={`ws-indicator ${wsConnected ? "ws-connected" : "ws-disconnected"}`}
              title={wsConnected ? "Connected" : "Disconnected"}
            />
          </h2>
          <div className="chat-sidebar-actions">
            <NotificationToggle />
            <button className="new-chat-btn" onClick={handleNewChat}>
              + New
            </button>
          </div>
        </div>
        <div className="chat-search">
          <input
            type="search"
            className="chat-search-input"
            placeholder="Search chats"
            aria-label="Search chats"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setQuery("");
            }}
          />
          {query && (
            <button
              type="button"
              className="chat-search-clear"
              aria-label="Clear search"
              title="Clear search"
              // Keep focus off the input, so the keyboard closes on mobile.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                setQuery("");
                (document.activeElement as HTMLElement | null)?.blur();
              }}
            >
              ✕
            </button>
          )}
        </div>
        {query.trim() ? (
          <SearchResults hits={hits} loading={searching} selected={selected} onPick={openHit} />
        ) : (
        <>
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
                onClick={() => {
                  setHighlight(null);
                  selectConv(c.id);
                }}
              >
                <div className="chat-list-title-row">
                  {st === "unread" && <span className="status-dot" title="Unread" />}
                  {st === "done" && <span className="status-check" title="Done">&#10003;</span>}
                  <span className="chat-list-title">{c.title || "New chat"}</span>
                </div>
                <div className="chat-list-meta">
                  <HarnessMarker name={c.harness} />
                  <span className="chat-list-time">{formatTime(c.updated_at)}</span>
                  {formatUsage(usage[c.id]) && (
                    <span className="chat-list-usage" title="Token usage / cost">
                      {formatUsage(usage[c.id])}
                    </span>
                  )}
                  {c.project && <span className="chat-list-project">{c.project}</span>}
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
                          onRename={() => setRenaming(c.id)}
                          onSetStatus={(s) => setStatus(c.id, s)}
                          onArchive={() => archive(c.id)}
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
        </>
        )}
      </aside>
      <SidebarResizer sidebar={sidebar} />
      <main className="chat-main">
        {active ? (
          <ChatThread
            key={active.id}
            conv={active}
            messages={messagesByConv[active.id] || []}
            processing={processingConvs.has(active.id)}
            steps={stepsByConv[active.id] || []}
            status={getStatus(statuses, active.id)}
            highlight={highlight}
            onBack={() => setSelected(null)}
            onSend={(text, files) => handleSend(active.id, text, files)}
            onRetry={(msg) => handleRetry(active.id, msg)}
            onStop={() => cancelProcessing(active.id).catch(console.error)}
            onShowSessionId={() => setSessionIdModal(active.claude_session_id || "(no session yet)")}
            onRename={() => setRenaming(active.id)}
            onSetStatus={(s) => setStatus(active.id, s)}
            onArchive={() => archive(active.id)}
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

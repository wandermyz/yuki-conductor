import { useCallback, useEffect, useRef, useState } from "react";
import { uploadFile } from "../chat/api";
import type { AttachmentRef, StreamStep } from "../chat/api";
import { HarnessIcon, StepList } from "../chat/shared";
import Markdown from "../Markdown";
import {
  cancelTask,
  commentOnTask,
  markTaskDone,
  moveTaskToTop,
  retryCleanup,
} from "./api";
import type { TaskAttachment, TaskComment, TaskDetail as Detail } from "./api";
import { StatusBadge } from "./common";
import { formatWhen } from "./format";

const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg)$/i;

function Attachments({ items }: { items: TaskAttachment[] }) {
  if (!items.length) return null;
  return (
    <div className="task-attachments">
      {items.map((a) =>
        IMAGE_RE.test(a.filename) ? (
          <a key={a.path} href={a.url} target="_blank" rel="noreferrer">
            <img src={a.url} alt={a.filename} />
          </a>
        ) : (
          <a key={a.path} className="file-link" href={a.url} target="_blank" rel="noreferrer">
            📎 {a.filename}
          </a>
        ),
      )}
    </div>
  );
}

function authorLabel(c: TaskComment, harness: string): React.ReactNode {
  switch (c.author) {
    case "user":
      return "You";
    case "agent":
      return (
        <>
          <HarnessIcon name={harness} /> Agent
        </>
      );
    case "github":
      return c.external_url ? (
        <a href={c.external_url} target="_blank" rel="noreferrer">
          {c.external_author ?? "GitHub"} on GitHub
        </a>
      ) : (
        `${c.external_author ?? "GitHub"} on GitHub`
      );
    default:
      return "System";
  }
}

/** Turn selected text into a Markdown quote block. */
function quoteBlock(text: string): string {
  return (
    text
      .trim()
      .split("\n")
      .map((l) => `> ${l}`)
      .join("\n") + "\n\n"
  );
}

interface QuoteTarget {
  text: string;
  commentId: number | null; // null: the task body
  top: number;
  left: number;
}

/**
 * Watch text selections inside the timeline. A selection that sits within a
 * single comment (or the task body) offers a floating "Quote" button.
 */
function useQuoteSelection(container: React.RefObject<HTMLDivElement | null>) {
  const [target, setTarget] = useState<QuoteTarget | null>(null);

  useEffect(() => {
    const onChange = () => {
      const sel = window.getSelection();
      const root = container.current;
      if (!sel || sel.isCollapsed || !root || !sel.rangeCount) {
        setTarget(null);
        return;
      }
      const range = sel.getRangeAt(0);
      const host = (n: Node | null) =>
        (n instanceof Element ? n : n?.parentElement)?.closest<HTMLElement>("[data-quote-id]");
      const start = host(range.startContainer);
      if (!start || start !== host(range.endContainer) || !root.contains(start)) {
        setTarget(null);
        return;
      }
      const rect = range.getBoundingClientRect();
      const box = root.getBoundingClientRect();
      const id = start.dataset.quoteId!;
      setTarget({
        text: sel.toString(),
        commentId: id === "body" ? null : Number(id),
        top: rect.bottom - box.top + root.scrollTop + 6,
        left: Math.min(Math.max(rect.left - box.left, 0), box.width - 90),
      });
    };
    document.addEventListener("selectionchange", onChange);
    return () => document.removeEventListener("selectionchange", onChange);
  }, [container]);

  return [target, () => setTarget(null)] as const;
}

export default function TaskDetail({
  task,
  steps,
  running,
  onBack,
  onEdit,
  onOpenTask,
  onChanged,
}: {
  task: Detail;
  steps: StreamStep[];
  running: boolean;
  onBack: () => void;
  onEdit: () => void;
  onOpenTask: (id: number) => void;
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [quoteId, setQuoteId] = useState<number | null>(null);
  const [uploads, setUploads] = useState<AttachmentRef[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const [quoteTarget, clearQuote] = useQuoteSelection(timelineRef);

  // Follow new comments and live steps, unless the user has scrolled up to
  // read. A newly opened task starts at the bottom, where the latest is.
  const followRef = useRef(true);
  useEffect(() => {
    followRef.current = true;
  }, [task.id]);
  useEffect(() => {
    const el = timelineRef.current;
    if (el && followRef.current) el.scrollTop = el.scrollHeight;
  }, [task.id, task.comments.length, steps.length, running]);
  const onTimelineScroll = () => {
    const el = timelineRef.current;
    if (el) followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      setError(null);
      try {
        await fn();
        onChanged();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [onChanged],
  );

  const quote = (text: string, commentId: number | null) => {
    setDraft((cur) => (cur && !cur.endsWith("\n") ? `${cur}\n\n` : cur) + quoteBlock(text));
    setQuoteId(commentId);
    clearQuote();
    window.getSelection()?.removeAllRanges();
    composerRef.current?.focus();
  };

  const send = async () => {
    if (!draft.trim() && !uploads.length) return;
    setBusy(true);
    await act(async () => {
      await commentOnTask(task.id, draft, quoteId, uploads.map((u) => u.id));
      setDraft("");
      setQuoteId(null);
      setUploads([]);
    });
    setBusy(false);
  };

  const addFiles = async (files: FileList | null) => {
    if (!files) return;
    try {
      const added = await Promise.all(Array.from(files).map(uploadFile));
      setUploads((cur) => [...cur, ...added]);
    } catch (e) {
      setError(String(e));
    }
  };

  const scrollToComment = (id: number) => {
    const el = timelineRef.current?.querySelector<HTMLElement>(`[data-quote-id="${id}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("task-comment-flash");
    setTimeout(() => el.classList.remove("task-comment-flash"), 1500);
  };

  const composerHint =
    task.status === "in_progress"
      ? "The agent is working. Your comment will reach it when this turn ends."
      : task.status === "done"
        ? "This task is done. Comments are kept but won't reopen it."
        : task.status === "todo"
          ? "Your comment will be included when the task is picked up."
          : "Commenting puts the task back in the queue.";

  return (
    <div className="task-detail">
      <button className="back-btn" onClick={onBack}>
        ‹ Tasks
      </button>

      <header className="task-detail-header">
        <h2>
          <span className="task-id">#{task.id}</span> {task.title}
        </h2>
        <div className="task-meta">
          <StatusBadge status={task.status} />
          <span>{task.project}</span>
          <span className="task-meta-harness">
            <HarnessIcon name={task.harness} /> {task.harness}
          </span>
          {task.branch && <code>{task.branch}</code>}
          {!task.branch && task.use_worktree && <span>worktree</span>}
          {task.pr_url && (
            <a href={task.pr_url} target="_blank" rel="noreferrer">
              PR #{task.pr_url.split("/").pop()}
            </a>
          )}
        </div>
        {task.parent && (
          <div className="task-links">
            Follow-up of{" "}
            <button className="link" onClick={() => onOpenTask(task.parent!.id)}>
              #{task.parent.id} {task.parent.title}
            </button>
          </div>
        )}
        {task.children.length > 0 && (
          <div className="task-links">
            Follow-ups:{" "}
            {task.children.map((c) => (
              <button key={c.id} className="link" onClick={() => onOpenTask(c.id)}>
                #{c.id} {c.title} <StatusBadge status={c.status} />
              </button>
            ))}
          </div>
        )}
        {!task.harness_enabled && task.status === "todo" && (
          <p className="task-warning">
            {task.harness} isn't enabled on this machine, so this task won't be picked up.
          </p>
        )}
        {task.cleanup_error && (
          <p className="task-warning">
            Cleanup failed: {task.cleanup_error}{" "}
            <button onClick={() => act(() => retryCleanup(task.id))}>Retry cleanup</button>
          </p>
        )}
        <div className="task-actions">
          {task.editable && <button onClick={onEdit}>Edit</button>}
          {task.status === "todo" && (
            <button onClick={() => act(() => moveTaskToTop(task.id))}>Move to top</button>
          )}
          {(task.status === "todo" || task.status === "in_progress") && (
            <button onClick={() => act(() => cancelTask(task.id))}>Cancel</button>
          )}
          {(task.status === "needs_input" || task.status === "ready_for_review") && (
            <button onClick={() => act(() => markTaskDone(task.id))}>Mark done</button>
          )}
        </div>
        {error && <p className="task-error">{error}</p>}
      </header>

      <div className="task-timeline" ref={timelineRef} onScroll={onTimelineScroll}>
        <article className="task-body" data-quote-id="body">
          {task.body.trim() ? (
            <Markdown text={task.body} />
          ) : (
            <p className="placeholder">No description.</p>
          )}
          <Attachments items={task.attachments} />
        </article>

        {task.comments.map((c) =>
          c.author === "system" ? (
            <div key={c.id} className="task-event">
              {c.body} · {formatWhen(c.created_at)}
            </div>
          ) : (
            <article key={c.id} className={`task-comment author-${c.author}`} data-quote-id={c.id}>
              <div className="task-comment-head">
                <span className="task-comment-author">{authorLabel(c, task.harness)}</span>
                <span className="task-comment-time">{formatWhen(c.created_at)}</span>
                {!c.delivered && <span className="task-pending">pending</span>}
                <button className="task-comment-quote" onClick={() => quote(c.body, c.id)}>
                  Quote
                </button>
              </div>
              {c.quote_comment_id !== null && (
                <button className="task-quote-link" onClick={() => scrollToComment(c.quote_comment_id!)}>
                  ↪ replying to #{c.quote_comment_id}
                </button>
              )}
              <Markdown text={c.body} />
              <Attachments items={c.attachments} />
            </article>
          ),
        )}

        {(running || steps.length > 0) && task.status === "in_progress" && (
          <StepList steps={steps} running={running} agent="Agent" />
        )}

        {quoteTarget && (
          <button
            className="task-quote-float"
            style={{ top: quoteTarget.top, left: quoteTarget.left }}
            // Act on press, not click: by the time a tap's click fires, iOS
            // has already cleared the selection and hidden this button.
            onPointerDown={(e) => {
              e.preventDefault();
              quote(quoteTarget.text, quoteTarget.commentId);
            }}
          >
            ❝ Quote
          </button>
        )}
      </div>

      <div className="task-composer">
        {quoteId !== null && (
          <div className="task-composer-quote">
            Replying to #{quoteId}{" "}
            <button className="link" onClick={() => setQuoteId(null)}>
              ×
            </button>
          </div>
        )}
        {uploads.length > 0 && (
          <div className="task-form-files">
            {uploads.map((u) => (
              <span key={u.id} className="task-file-chip">
                📎 {u.filename}
                <button onClick={() => setUploads((cur) => cur.filter((x) => x.id !== u.id))}>×</button>
              </span>
            ))}
          </div>
        )}
        <textarea
          ref={composerRef}
          value={draft}
          rows={3}
          placeholder="Comment (Markdown). Select text above to quote it."
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="task-composer-row">
          <label className="task-attach-btn" title="Attach files">
            📎
            <input type="file" multiple hidden onChange={(e) => addFiles(e.target.files)} />
          </label>
          <span className="task-composer-hint">{composerHint}</span>
          <button
            className="primary"
            disabled={busy || (!draft.trim() && !uploads.length)}
            onClick={send}
          >
            Comment
          </button>
        </div>
      </div>
    </div>
  );
}

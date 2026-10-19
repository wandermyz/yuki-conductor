import { useEffect, useState } from "react";
import { uploadFile } from "../chat/api";
import type { AttachmentRef, HarnessInfo, Project } from "../chat/api";
import { HarnessIcon } from "../chat/shared";
import Markdown from "../Markdown";
import { createTask, editTask } from "./api";
import type { Task, TaskInput } from "./api";
import { DRAFT_KEY, loadDraft } from "./draft";

/**
 * Create a task, or edit one nobody has picked up yet. A new task's agent and
 * project were chosen in the picker (`picked`) or come from its saved draft,
 * so only an edit can change them here.
 */
export default function TaskForm({
  task,
  picked,
  harnesses,
  projects,
  onSaved,
  onCancel,
}: {
  task?: Task;
  picked?: { harness: string | null; project: string };
  harnesses: HarnessInfo[];
  projects: Project[];
  onSaved: (task: Task) => void;
  onCancel: () => void;
}) {
  const [draft] = useState(() => (task ? null : loadDraft()));
  const [input, setInput] = useState<TaskInput>(
    () =>
      draft?.input ?? {
        title: task?.title ?? "",
        body: task?.body ?? "",
        project: task?.project ?? picked?.project ?? "",
        // Empty means the server's default harness.
        harness: task?.harness ?? picked?.harness ?? "",
        use_worktree: task?.use_worktree ?? true,
      },
  );
  const [uploads, setUploads] = useState<AttachmentRef[]>(draft?.uploads ?? []);
  const [preview, setPreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasDraft = !task && (!!input.title.trim() || !!input.body.trim() || uploads.length > 0);

  useEffect(() => {
    if (task) return;
    if (hasDraft) localStorage.setItem(DRAFT_KEY, JSON.stringify({ input, uploads }));
    else localStorage.removeItem(DRAFT_KEY);
  }, [task, hasDraft, input, uploads]);

  const discard = () => {
    localStorage.removeItem(DRAFT_KEY);
    onCancel();
  };

  const set = <K extends keyof TaskInput>(key: K, value: TaskInput[K]) =>
    setInput((cur) => ({ ...cur, [key]: value }));

  const addFiles = async (files: FileList | null) => {
    if (!files) return;
    try {
      const added = await Promise.all(Array.from(files).map(uploadFile));
      setUploads((cur) => [...cur, ...added]);
    } catch (e) {
      setError(String(e));
    }
  };

  const save = async () => {
    if (!input.title.trim() || !input.project) return;
    setSaving(true);
    setError(null);
    try {
      const saved = task
        ? await editTask(task.id, input)
        : await createTask({ ...input, attachment_ids: uploads.map((u) => u.id) });
      if (!task) localStorage.removeItem(DRAFT_KEY);
      onSaved(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay">
      <div className="modal-content task-form">
        <h3 className="modal-title">
          {task ? `Edit task #${task.id}` : "New task"}
          {!task && (
            <span className="picker-step-context">
              {harnesses.length > 1 && (
                <>
                  <HarnessIcon name={input.harness} />
                  {harnesses.find((h) => h.name === input.harness)?.label ?? input.harness}
                  {" · "}
                </>
              )}
              {input.project}
            </span>
          )}
        </h3>

        <input
          className="task-form-title"
          placeholder="Title"
          value={input.title}
          autoFocus
          onChange={(e) => set("title", e.target.value)}
        />

        <div className="task-form-tabs">
          <button className={!preview ? "active" : ""} onClick={() => setPreview(false)}>
            Write
          </button>
          <button className={preview ? "active" : ""} onClick={() => setPreview(true)}>
            Preview
          </button>
        </div>
        {preview ? (
          <div className="task-form-preview">
            {input.body.trim() ? <Markdown text={input.body} /> : <p className="placeholder">Nothing to preview</p>}
          </div>
        ) : (
          <textarea
            className="task-form-body"
            placeholder="What should the agent do? Markdown and ```mermaid diagrams work."
            value={input.body}
            rows={8}
            onChange={(e) => set("body", e.target.value)}
          />
        )}

        {task && (
          <>
            <label className="task-form-field">
              <span>Project</span>
              <select value={input.project} onChange={(e) => set("project", e.target.value)}>
                {projects.length === 0 && <option value="">No projects — add one in Projects</option>}
                {projects.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>

            {harnesses.length > 1 && (
              <div className="task-form-field">
                <span>Agent</span>
                <div className="task-form-harnesses">
                  {harnesses.map((h) => (
                    <button
                      key={h.name}
                      className={input.harness === h.name ? "active" : ""}
                      onClick={() => set("harness", h.name)}
                    >
                      <HarnessIcon name={h.name} />
                      {h.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        <label className="task-form-check">
          <input
            type="checkbox"
            checked={input.use_worktree}
            onChange={(e) => set("use_worktree", e.target.checked)}
          />
          <span>
            Work in a git worktree
            <small>A fresh branch from the default branch. Leave off for read-only tasks.</small>
          </span>
        </label>

        {!task && (
          <div className="task-form-files">
            {uploads.map((u) => (
              <span key={u.id} className="task-file-chip">
                📎 {u.filename}
                <button onClick={() => setUploads((cur) => cur.filter((x) => x.id !== u.id))}>×</button>
              </span>
            ))}
            <label className="task-attach-btn">
              Attach files
              <input type="file" multiple hidden onChange={(e) => addFiles(e.target.files)} />
            </label>
          </div>
        )}

        {error && <p className="task-error">{error}</p>}

        <div className="modal-actions task-form-actions">
          {hasDraft && (
            <button className="task-form-discard" onClick={discard}>
              Discard draft
            </button>
          )}
          <button onClick={onCancel}>Cancel</button>
          <button
            className="primary"
            disabled={saving || !input.title.trim() || !input.project}
            onClick={save}
          >
            {saving ? "Saving…" : task ? "Save" : "Create task"}
          </button>
        </div>
      </div>
    </div>
  );
}

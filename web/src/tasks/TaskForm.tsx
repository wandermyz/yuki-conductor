import { useEffect, useState } from "react";
import { uploadFile } from "../chat/api";
import type { AttachmentRef, HarnessInfo, Project } from "../chat/api";
import { HarnessIcon, ModelPicker, NewSessionPicker } from "../chat/shared";
import Markdown from "../Markdown";
import { imageMarkers, insertAt, pastedFiles, withoutMarker } from "../paste";
import { createTask, editTask } from "./api";
import type { Task, TaskInput } from "./api";
import { DRAFT_KEY, loadDraft } from "./draft";

/**
 * Create a task, or edit one nobody has picked up yet. A new task's agent and
 * project were chosen in the picker (`picked`) or come from its saved draft;
 * either way the title shows them, and clicking it reopens the same picker.
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
        model: task?.model ?? null,
        use_worktree: task?.use_worktree ?? true,
      },
  );
  const [uploads, setUploads] = useState<AttachmentRef[]>(draft?.uploads ?? []);
  const [preview, setPreview] = useState(false);
  const [picking, setPicking] = useState(false);
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

  /** Upload `files`; pasted images also get an inline marker at `markerAt`. */
  const addFiles = async (files: File[], markerAt?: number) => {
    try {
      const added = await Promise.all(files.map(uploadFile));
      setUploads((cur) => [...cur, ...added]);
      if (markerAt !== undefined)
        setInput((cur) => ({ ...cur, body: insertAt(cur.body, markerAt, imageMarkers(added)) }));
    } catch (e) {
      setError(String(e));
    }
  };

  const removeUpload = (u: AttachmentRef) => {
    setUploads((cur) => cur.filter((x) => x.id !== u.id));
    setInput((cur) => ({ ...cur, body: withoutMarker(cur.body, u) }));
  };

  const save = async () => {
    if (!input.title.trim() || !input.project) return;
    setSaving(true);
    setError(null);
    try {
      const attachment_ids = uploads.map((u) => u.id);
      const saved = task
        ? await editTask(task.id, { ...input, attachment_ids })
        : await createTask({ ...input, attachment_ids });
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
          <button
            className="picker-step-context task-form-repick"
            title="Change agent or project"
            onClick={() => setPicking(true)}
          >
            {harnesses.length > 1 && (
              <>
                <HarnessIcon name={input.harness} />
                {harnesses.find((h) => h.name === input.harness)?.label ?? input.harness}
                {" · "}
              </>
            )}
            {input.project || "No project"}
            <span className="task-form-repick-hint">Change</span>
          </button>
        </h3>

        <ModelPicker
          harness={harnesses.find((h) => h.name === input.harness) ?? (!input.harness ? harnesses[0] : undefined)}
          model={input.model ?? null}
          onChange={(model) => set("model", model)}
          title="Model for this task"
        />

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
            placeholder="What should the agent do? Markdown and ```mermaid diagrams work. Paste images or files to attach them."
            value={input.body}
            rows={8}
            onChange={(e) => set("body", e.target.value)}
            onPaste={(e) => {
              const files = pastedFiles(e);
              if (!files.length) return;
              e.preventDefault();
              addFiles(files, e.currentTarget.selectionStart);
            }}
          />
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

        <div className="task-form-files">
          {uploads.map((u) => (
            <span key={u.id} className="task-file-chip">
              📎 {u.filename}
              <button onClick={() => removeUpload(u)}>×</button>
            </span>
          ))}
          <label className="task-attach-btn">
            Attach files
            <input type="file" multiple hidden onChange={(e) => addFiles(Array.from(e.target.files ?? []))} />
          </label>
        </div>

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
      {picking && (
        <NewSessionPicker
          harnesses={harnesses}
          projects={projects}
          allowNoProject={false}
          onPick={(harness, project) => {
            // `harness` is null only without a harness list: keep the current one.
            setInput((cur) => ({ ...cur, harness: harness ?? cur.harness, model: harness && harness !== cur.harness ? null : cur.model, project: project ?? cur.project }));
            setPicking(false);
          }}
          onCancel={() => setPicking(false)}
        />
      )}
    </div>
  );
}

import { useEffect, useState } from "react";
import { listHarnesses, listProjects, uploadFile } from "../chat/api";
import type { AttachmentRef, HarnessInfo, Project } from "../chat/api";
import { HarnessIcon } from "../chat/shared";
import Markdown from "../Markdown";
import { createTask, editTask } from "./api";
import type { Task, TaskInput } from "./api";

/** Create a task, or edit one nobody has picked up yet. */
export default function TaskForm({
  task,
  onSaved,
  onCancel,
}: {
  task?: Task;
  onSaved: (task: Task) => void;
  onCancel: () => void;
}) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [harnesses, setHarnesses] = useState<HarnessInfo[]>([]);
  const [input, setInput] = useState<TaskInput>({
    title: task?.title ?? "",
    body: task?.body ?? "",
    project: task?.project ?? "",
    harness: task?.harness ?? "",
    use_worktree: task?.use_worktree ?? true,
  });
  const [uploads, setUploads] = useState<AttachmentRef[]>([]);
  const [preview, setPreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listProjects()
      .then((ps) => {
        setProjects(ps);
        setInput((cur) => (cur.project || !ps.length ? cur : { ...cur, project: ps[0].name }));
      })
      .catch(console.error);
    listHarnesses()
      .then(({ default: def, harnesses: hs }) => {
        setHarnesses(hs.filter((h) => h.available));
        setInput((cur) => (cur.harness ? cur : { ...cur, harness: def }));
      })
      .catch(console.error);
  }, []);

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
      onSaved(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-content task-form" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">{task ? `Edit task #${task.id}` : "New task"}</h3>

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

/** Typed client for the taskboard API (src/yuki_conductor/taskboard/routes.py). */
import type { StreamStep } from "../chat/api";

export type TaskStatus = "todo" | "in_progress" | "needs_input" | "ready_for_review" | "done";

export type PrState = "OPEN" | "DRAFT" | "MERGED" | "CLOSED";

export const STATUS_LABELS: Record<TaskStatus, string> = {
  todo: "To Do",
  in_progress: "In Progress",
  needs_input: "Needs Input",
  ready_for_review: "Ready for Review",
  done: "Done",
};

export interface TaskAttachment {
  path: string;
  filename: string;
  url: string;
}

export interface TaskRef {
  id: number;
  title: string;
  status: TaskStatus;
}

export interface Task {
  id: number;
  title: string;
  body: string;
  attachments: TaskAttachment[];
  project: string;
  harness: string;
  model: string | null;
  harness_enabled: boolean;
  use_worktree: boolean;
  status: TaskStatus;
  status_label: string;
  sort_order: number;
  session_id: string | null;
  worktree_path: string | null;
  branch: string | null;
  pr_url: string | null;
  /** As GitHub last showed it; null until first checked. */
  pr_state: PrState | null;
  parent_task_id: number | null;
  cleanup_error: string | null;
  editable: boolean;
  created_at: number;
  updated_at: number;
  started_at: number | null;
  done_at: number | null;
  comment_count: number | null;
}

export interface TaskComment {
  id: number;
  task_id: number;
  author: "user" | "agent" | "system" | "github";
  body: string;
  quote_comment_id: number | null;
  attachments: TaskAttachment[];
  delivered: boolean;
  external_url: string | null;
  external_author: string | null;
  created_at: number;
}

export interface TaskDetail extends Task {
  comments: TaskComment[];
  children: TaskRef[];
  parent: TaskRef | null;
}

export interface BoardState {
  paused: boolean;
  running: Record<string, number>;
  limits: Record<string, number>;
}

export interface TaskInput {
  title: string;
  project: string;
  harness: string;
  model: string | null;
  use_worktree: boolean;
  body: string;
}

async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = `${r.status}`;
    try {
      const data = await r.json();
      if (typeof data.detail === "string") detail = data.detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return r.json();
}

/** `old`: include tasks done over 30 days ago. Archived tasks never come back. */
export const listTasks = (old = false) => call<Task[]>("GET", `/api/tasks${old ? "?old=true" : ""}`);

/** One task matching a search: its best match. `comment_id` null means the
 *  title or description matched. `snippet` marks terms with \x02…\x03. */
export interface TaskSearchHit {
  task_id: number;
  title: string;
  status: TaskStatus;
  comment_id: number | null;
  snippet: string;
  score: number;
}

export const searchTasks = (q: string) =>
  call<TaskSearchHit[]>("GET", `/api/tasks/search?q=${encodeURIComponent(q)}`);

export const getTask = (id: number) => call<TaskDetail>("GET", `/api/tasks/${id}`);

export const createTask = (input: TaskInput & { attachment_ids: string[] }) =>
  call<Task>("POST", "/api/tasks", input);

export const editTask = (id: number, fields: Partial<TaskInput> & { attachment_ids?: string[] }) =>
  call<Task>("PATCH", `/api/tasks/${id}`, fields);

export const commentOnTask = (
  id: number,
  body: string,
  quoteCommentId: number | null,
  attachmentIds: string[],
) =>
  call<TaskComment>("POST", `/api/tasks/${id}/comments`, {
    body,
    quote_comment_id: quoteCommentId,
    attachment_ids: attachmentIds,
  });

/** Only a user comment that's still pending (not yet sent to the agent). */
export const deleteTaskComment = (id: number, commentId: number) =>
  call<{ ok: boolean }>("DELETE", `/api/tasks/${id}/comments/${commentId}`);

/** Ask GitHub for this task's PR state now; waits for the answer. */
export const refreshTaskPr = (id: number) => call<Task>("POST", `/api/tasks/${id}/pr-refresh`);
export const cancelTask = (id: number) => call<Task>("POST", `/api/tasks/${id}/cancel`);
export const markTaskDone = (id: number) => call<Task>("POST", `/api/tasks/${id}/done`);
export const reopenTask = (id: number) => call<Task>("POST", `/api/tasks/${id}/reopen`);
export const retryCleanup = (id: number) => call<Task>("POST", `/api/tasks/${id}/cleanup`);
export const archiveTask = (id: number) => call<{ ok: boolean }>("POST", `/api/tasks/${id}/archive`);
export const moveTaskToTop = (id: number) => call<Task>("POST", `/api/tasks/${id}/top`);
export const reorderTasks = (ids: number[]) => call<{ ok: boolean }>("PUT", "/api/tasks/order", { ids });

export const getBoardState = () => call<BoardState>("GET", "/api/taskboard/state");
export const setBoardPaused = (paused: boolean) =>
  call<BoardState>("PUT", "/api/taskboard/state", { paused });
/** Check every Ready for Review task's PR once, in the background. */
export const refreshPrs = () => call<{ started: boolean }>("POST", "/api/taskboard/pr-refresh");

/** Buffered live steps of the task's current (or last) turn. */
export const fetchTaskSteps = (id: number) => call<StreamStep[]>("GET", `/api/tasks/${id}/steps`);

/** The chat socket keys a task's live steps like a conversation. */
export const stepsKey = (id: number) => `task:${id}`;

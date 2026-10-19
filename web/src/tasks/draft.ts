import type { AttachmentRef } from "../chat/api";
import type { TaskInput } from "./api";

/** A new task in progress survives Cancel, a dismissed dialog and a reload. */
export const DRAFT_KEY = "task-draft";

export interface Draft {
  input: TaskInput;
  uploads: AttachmentRef[];
}

export function loadDraft(): Draft | null {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    return raw ? (JSON.parse(raw) as Draft) : null;
  } catch {
    return null;
  }
}

import type { TaskStatus } from "./api";
import { STATUS_LABELS } from "./api";

export function StatusBadge({ status }: { status: TaskStatus }) {
  return <span className={`task-status status-${status}`}>{STATUS_LABELS[status]}</span>;
}

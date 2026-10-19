import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { DragEndEvent } from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { fetchProcessing, subscribeChatSocket } from "../chat/api";
import type { StreamStep } from "../chat/api";
import { HarnessIcon } from "../chat/shared";
import {
  fetchTaskSteps,
  getBoardState,
  getTask,
  listTasks,
  reorderTasks,
  setBoardPaused,
  stepsKey,
} from "./api";
import type { BoardState, Task, TaskDetail as Detail, TaskStatus } from "./api";
import { StatusBadge } from "./common";
import { formatWhen } from "./format";
import TaskDetail from "./TaskDetail";
import TaskForm from "./TaskForm";
import "./tasks.css";

type Filter = "active" | TaskStatus;

const FILTERS: { key: Filter; label: string }[] = [
  { key: "active", label: "Active" },
  { key: "needs_input", label: "Needs Input" },
  { key: "ready_for_review", label: "Review" },
  { key: "in_progress", label: "In Progress" },
  { key: "todo", label: "To Do" },
  { key: "done", label: "Done" },
];

/** The order statuses are grouped in: what needs the user first. */
const STATUS_ORDER: TaskStatus[] = ["needs_input", "ready_for_review", "in_progress", "todo", "done"];

function sortForDisplay(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => {
    const s = STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
    if (s !== 0) return s;
    if (a.status === "todo") return a.sort_order - b.sort_order;
    return b.updated_at - a.updated_at;
  });
}

function TaskRow({
  task,
  active,
  draggable,
  onOpen,
}: {
  task: Task;
  active: boolean;
  draggable: boolean;
  onOpen: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: task.id,
    disabled: !draggable,
  });
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`task-row ${active ? "active" : ""} ${isDragging ? "dragging" : ""}`}
      onClick={onOpen}
    >
      {draggable && (
        <span
          className="task-drag"
          title="Drag to reorder"
          onClick={(e) => e.stopPropagation()}
          {...attributes}
          {...listeners}
        >
          ⋮⋮
        </span>
      )}
      <div className="task-row-main">
        <span className="task-row-title">
          <span className="task-id">#{task.id}</span> {task.title}
        </span>
        <span className="task-row-meta">
          <StatusBadge status={task.status} />
          <HarnessIcon name={task.harness} />
          <span>{task.project}</span>
          {task.pr_url && <span title={task.pr_url}>PR</span>}
          {task.comment_count ? <span>💬 {task.comment_count}</span> : null}
          <span className="task-row-time">{formatWhen(task.updated_at)}</span>
        </span>
        {!task.harness_enabled && task.status === "todo" && (
          <span className="task-row-warning">{task.harness} not enabled here</span>
        )}
      </div>
    </li>
  );
}

export default function TasksView({
  selectedId,
  onSelectId,
}: {
  selectedId: number | null;
  onSelectId: (id: number | null) => void;
}) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [board, setBoard] = useState<BoardState | null>(null);
  const [filter, setFilter] = useState<Filter>("active");
  const [showArchived, setShowArchived] = useState(false);
  const [detail, setDetail] = useState<Detail | null>(null);
  // Live steps of a task's turn, tagged with the task they belong to so a
  // switch of selection never shows another task's stream.
  const [live, setLive] = useState<{ id: number | null; steps: StreamStep[]; running: boolean }>({
    id: null,
    steps: [],
    running: false,
  });
  const [form, setForm] = useState<"new" | "edit" | null>(null);
  const selectedRef = useRef(selectedId);
  useEffect(() => {
    selectedRef.current = selectedId;
  }, [selectedId]);

  const refreshList = useCallback(() => {
    listTasks(showArchived)
      .then(setTasks)
      .catch(console.error)
      .finally(() => setLoading(false));
    getBoardState().then(setBoard).catch(console.error);
  }, [showArchived]);

  const refreshDetail = useCallback(() => {
    const id = selectedRef.current;
    if (id === null) return;
    getTask(id)
      .then((t) => {
        if (selectedRef.current === t.id) setDetail(t);
      })
      .catch(() => {
        if (selectedRef.current === id) setDetail(null);
      });
  }, []);

  /** Replay the open task's buffered steps, and whether its turn is running.
   *  The buffer outlives a turn, so "running" comes from the processing map. */
  const refreshLive = useCallback(() => {
    const id = selectedRef.current;
    if (id === null) return;
    Promise.all([fetchTaskSteps(id), fetchProcessing()])
      .then(([steps, processing]) => {
        if (selectedRef.current !== id) return; // switched away meanwhile
        setLive({ id, steps, running: stepsKey(id) in processing });
      })
      .catch(console.error);
  }, []);

  useEffect(() => {
    refreshList();
  }, [refreshList]);

  // Load the selected task and replay its live steps.
  useEffect(() => {
    if (selectedId === null) return;
    refreshDetail();
    refreshLive();
  }, [selectedId, refreshDetail, refreshLive]);

  // Live updates: task changes refetch; steps stream for the open task.
  useEffect(() => {
    let pending: ReturnType<typeof setTimeout> | null = null;
    const changed = new Set<number>();
    const unsubscribe = subscribeChatSocket({
      onEvent: (e) => {
        if (e.type === "task") {
          // Coalesce bursts (a turn's end writes several changes at once),
          // remembering every task they touched.
          changed.add(e.id);
          if (pending) clearTimeout(pending);
          pending = setTimeout(() => {
            pending = null;
            refreshList();
            if (selectedRef.current !== null && changed.has(selectedRef.current)) refreshDetail();
            changed.clear();
          }, 150);
          return;
        }
        const id = selectedRef.current;
        if (id === null || !("conversation_id" in e) || e.conversation_id !== stepsKey(id)) return;
        if (e.type === "step") {
          setLive((cur) => {
            const steps = cur.id === id ? cur.steps : [];
            if (steps.some((s) => s.seq === e.step.seq)) return cur;
            return { id, steps: [...steps, e.step], running: true };
          });
        } else if (e.type === "processing") {
          setLive((cur) => ({
            id,
            steps: e.on || cur.id !== id ? [] : cur.steps,
            running: e.on,
          }));
        }
      },
      // Anything may have happened while disconnected: catch up fully.
      onOpen: () => {
        refreshList();
        refreshDetail();
        refreshLive();
      },
    });
    return () => {
      if (pending) clearTimeout(pending);
      unsubscribe();
    };
  }, [refreshList, refreshDetail, refreshLive]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { active: 0 };
    for (const t of tasks) {
      c[t.status] = (c[t.status] ?? 0) + 1;
      if (t.status !== "done") c.active += 1;
    }
    return c;
  }, [tasks]);

  const shown = useMemo(() => {
    const filtered = tasks.filter((t) =>
      filter === "active" ? t.status !== "done" : t.status === filter,
    );
    return sortForDisplay(filtered);
  }, [tasks, filter]);

  const shownDetail = detail && detail.id === selectedId ? detail : null;

  const todoIds = shown.filter((t) => t.status === "todo").map((t) => t.id);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 150, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const order = arrayMove(todoIds, todoIds.indexOf(Number(active.id)), todoIds.indexOf(Number(over.id)));
    // Optimistic: renumber locally so the row doesn't jump back while saving.
    setTasks((cur) => {
      const slots = cur
        .filter((t) => order.includes(t.id))
        .map((t) => t.sort_order)
        .sort((a, b) => a - b);
      const next = new Map(order.map((id, i) => [id, slots[i]]));
      return cur.map((t) => (next.has(t.id) ? { ...t, sort_order: next.get(t.id)! } : t));
    });
    reorderTasks(order).catch((err) => {
      console.error(err);
      refreshList();
    });
  };

  const togglePause = () => {
    if (!board) return;
    setBoardPaused(!board.paused).then(setBoard).catch(console.error);
  };

  return (
    <div className={`app tasks-view ${selectedId !== null ? "detail-open" : ""}`}>
      <aside className="sidebar task-sidebar">
        <div className="sidebar-header task-sidebar-header">
          <h2>Tasks</h2>
          <button className="primary" onClick={() => setForm("new")}>
            + New
          </button>
        </div>

        {board && (
          <div className={`task-board-state ${board.paused ? "paused" : ""}`}>
            <button className="task-pause" onClick={togglePause}>
              {board.paused ? "▶ Resume" : "⏸ Pause"}
            </button>
            <span className="task-capacity">
              {Object.keys(board.limits).map((h) => (
                <span key={h} title={`${h}: running / limit`}>
                  <HarnessIcon name={h} /> {board.running[h] ?? 0}/{board.limits[h]}
                </span>
              ))}
            </span>
          </div>
        )}
        {board?.paused && <div className="task-paused-banner">Paused — no new tasks will start</div>}

        <div className="task-filters" role="tablist">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              role="tab"
              aria-selected={filter === f.key}
              className={filter === f.key ? "active" : ""}
              onClick={() => setFilter(f.key)}
            >
              {f.label} <span className="task-filter-count">{counts[f.key] ?? 0}</span>
            </button>
          ))}
        </div>

        {loading && <p className="placeholder">Loading…</p>}
        {!loading && shown.length === 0 && (
          <p className="placeholder">
            {filter === "active" ? "Nothing queued. Create a task to get an agent on it." : "No tasks here."}
          </p>
        )}

        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onDragEnd}>
          <SortableContext items={todoIds} strategy={verticalListSortingStrategy}>
            <ul className="task-list">
              {shown.map((t) => (
                <TaskRow
                  key={t.id}
                  task={t}
                  active={t.id === selectedId}
                  draggable={t.status === "todo" && todoIds.length > 1}
                  onOpen={() => onSelectId(t.id)}
                />
              ))}
            </ul>
          </SortableContext>
        </DndContext>

        {filter === "done" || filter === "active" ? (
          <label className="task-archived-toggle">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={(e) => setShowArchived(e.target.checked)}
            />
            Show archived (done over 30 days ago)
          </label>
        ) : null}
      </aside>

      <main className="content task-content">
        {shownDetail ? (
          <TaskDetail
            // Fresh composer state (draft, quote, uploads) per task.
            key={shownDetail.id}
            task={shownDetail}
            steps={live.id === selectedId ? live.steps : []}
            running={live.id === selectedId && live.running}
            onBack={() => onSelectId(null)}
            onEdit={() => setForm("edit")}
            onOpenTask={onSelectId}
            onChanged={() => {
              refreshDetail();
              refreshList();
            }}
          />
        ) : (
          <p className="placeholder">Select a task, or create one.</p>
        )}
      </main>

      {form && (
        <TaskForm
          task={form === "edit" && shownDetail ? shownDetail : undefined}
          onCancel={() => setForm(null)}
          onSaved={(t) => {
            setForm(null);
            refreshList();
            if (t.id === selectedId) refreshDetail();
            else onSelectId(t.id);
          }}
        />
      )}
    </div>
  );
}

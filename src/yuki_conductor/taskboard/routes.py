"""HTTP API for the taskboard, mounted by `web_server.create_api`."""

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from yuki_conductor import taskboard
from yuki_conductor.harness import HARNESSES
from yuki_conductor.messaging.web_platform import resolve_file
from yuki_conductor.task_store import STATUS_LABELS, Task, TaskComment
from yuki_conductor.taskboard import actions, live
from yuki_conductor.taskboard.actions import BoardError

router = APIRouter()


def _attachments(task_id: int, paths: list[str]) -> list[dict]:
    return [
        {
            "path": p,
            "filename": Path(p).name,
            "url": f"/api/tasks/{task_id}/file?path={quote(p)}",
        }
        for p in paths
    ]


def serialize_task(task: Task, *, comment_count: int | None = None) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "attachments": _attachments(task.id, task.attachments),
        "project": task.project,
        "harness": task.harness,
        "harness_enabled": task.harness in HARNESSES,
        "use_worktree": task.use_worktree,
        "status": task.status,
        "status_label": STATUS_LABELS[task.status],
        "sort_order": task.sort_order,
        "session_id": task.session_id,
        "worktree_path": task.worktree_path,
        "branch": task.branch,
        "pr_url": task.pr_url,
        "pr_state": task.pr_state,
        "parent_task_id": task.parent_task_id,
        "cleanup_error": task.cleanup_error,
        "editable": task.editable,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "done_at": task.done_at,
        "comment_count": comment_count,
    }


def serialize_comment(comment: TaskComment) -> dict:
    return {
        "id": comment.id,
        "task_id": comment.task_id,
        "author": comment.author,
        "body": comment.body,
        "quote_comment_id": comment.quote_comment_id,
        "attachments": _attachments(comment.task_id, comment.attachments),
        "delivered": comment.delivered,
        "external_url": comment.external_url,
        "external_author": comment.external_author,
        "created_at": comment.created_at,
    }


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except BoardError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _resolve_uploads(ids: list[str]) -> list[str]:
    """Web uploads arrive as `/api/uploads` ids; the board stores paths."""
    paths = []
    for file_id in ids:
        path = resolve_file(file_id)
        if path is None:
            raise HTTPException(status_code=400, detail=f"Unknown upload {file_id!r}")
        paths.append(str(path))
    return paths


def _paths(attachments: list[str], attachment_ids: list[str]) -> list[str]:
    for p in attachments:
        if not Path(p).is_absolute() or not Path(p).is_file():
            raise HTTPException(status_code=400, detail=f"Not a file: {p}")
    return [*attachments, *_resolve_uploads(attachment_ids)]


class TaskCreate(BaseModel):
    title: str
    project: str
    harness: str | None = None
    use_worktree: bool = True
    body: str = ""
    attachments: list[str] = []  # absolute paths (CLI)
    attachment_ids: list[str] = []  # /api/uploads ids (web)
    parent_task_id: int | None = None


class TaskEdit(BaseModel):
    title: str | None = None
    project: str | None = None
    harness: str | None = None
    use_worktree: bool | None = None
    body: str | None = None
    attachments: list[str] | None = None  # the full new list of paths
    attachment_ids: list[str] = []  # /api/uploads ids (web), added to the list


class CommentCreate(BaseModel):
    body: str = ""
    author: str = "user"
    quote_comment_id: int | None = None
    attachments: list[str] = []
    attachment_ids: list[str] = []
    notify: bool = False


class AskBody(BaseModel):
    question: str


class PrBody(BaseModel):
    url: str


class OrderBody(BaseModel):
    ids: list[int]


class StateBody(BaseModel):
    paused: bool


@router.get("/api/tasks")
def list_tasks(old: bool = False):
    tasks = taskboard.store.list_tasks(include_old=old)
    counts = taskboard.store.comment_counts()
    return [serialize_task(t, comment_count=counts.get(t.id, 0)) for t in tasks]


@router.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate):
    task = _call(
        actions.create,
        title=body.title,
        project=body.project,
        harness=body.harness,
        use_worktree=body.use_worktree,
        body=body.body,
        attachments=_paths(body.attachments, body.attachment_ids),
        parent_task_id=body.parent_task_id,
    )
    return serialize_task(task, comment_count=0)


@router.put("/api/tasks/order")
def reorder_tasks(body: OrderBody):
    _call(actions.reorder, body.ids)
    return {"ok": True}


@router.get("/api/tasks/{task_id}")
def get_task(task_id: int):
    task = _call(actions.get, task_id)
    comments = taskboard.store.comments(task_id)
    data = serialize_task(task, comment_count=sum(c.author != "system" for c in comments))
    data["comments"] = [serialize_comment(c) for c in comments]
    data["children"] = [
        {"id": c.id, "title": c.title, "status": c.status}
        for c in taskboard.store.children(task_id)
    ]
    parent = taskboard.store.get(task.parent_task_id) if task.parent_task_id else None
    data["parent"] = (
        {"id": parent.id, "title": parent.title, "status": parent.status}
        if parent and parent.archived_at is None
        else None
    )
    return data


@router.patch("/api/tasks/{task_id}")
def edit_task(task_id: int, body: TaskEdit):
    fields = {k: getattr(body, k) for k in body.model_fields_set - {"attachment_ids"}}
    nulls = [k for k, v in fields.items() if v is None and k != "attachments"]
    if nulls:
        raise HTTPException(status_code=400, detail=f"Can't clear: {', '.join(nulls)}")
    if "attachments" in fields or body.attachment_ids:
        current = fields.get("attachments")
        if current is None:
            current = _call(actions.get, task_id).attachments
        fields["attachments"] = _paths(current, body.attachment_ids)
    return serialize_task(_call(actions.edit, task_id, **fields))


@router.post("/api/tasks/{task_id}/comments", status_code=201)
def comment_on_task(task_id: int, body: CommentCreate):
    comment = _call(
        actions.add_comment,
        task_id,
        body.body,
        author=body.author,
        quote_comment_id=body.quote_comment_id,
        attachments=_paths(body.attachments, body.attachment_ids),
        notify_user=body.notify,
    )
    return serialize_comment(comment)


@router.post("/api/tasks/{task_id}/ask", status_code=201)
def ask_on_task(task_id: int, body: AskBody):
    return serialize_comment(_call(actions.ask, task_id, body.question))


@router.post("/api/tasks/{task_id}/pr")
def set_task_pr(task_id: int, body: PrBody):
    return serialize_task(_call(actions.set_pr, task_id, body.url))


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int):
    return serialize_task(_call(actions.cancel, task_id))


@router.post("/api/tasks/{task_id}/done")
def finish_task(task_id: int):
    return serialize_task(_call(actions.mark_done, task_id))


@router.post("/api/tasks/{task_id}/reopen")
def reopen_task(task_id: int):
    return serialize_task(_call(actions.reopen, task_id))


@router.post("/api/tasks/{task_id}/cleanup")
def retry_task_cleanup(task_id: int):
    return serialize_task(_call(actions.retry_cleanup, task_id))


@router.post("/api/tasks/{task_id}/archive")
def archive_task(task_id: int):
    _call(actions.archive, task_id)
    return {"ok": True}


@router.post("/api/tasks/{task_id}/top")
def move_task_to_top(task_id: int):
    return serialize_task(_call(actions.move_to_top, task_id))


@router.get("/api/tasks/{task_id}/steps")
def task_steps(task_id: int):
    _call(actions.get, task_id)
    return live.steps(task_id)


@router.get("/api/tasks/{task_id}/file")
def task_file(task_id: int, path: str):
    # Only files the task itself references, never an arbitrary path.
    if path not in _call(actions.attachment_paths, task_id) or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, filename=Path(path).name, content_disposition_type="inline")


@router.get("/api/taskboard/state")
def board_state():
    return actions.board_status()


@router.put("/api/taskboard/state")
def set_board_state(body: StateBody):
    return actions.set_paused(body.paused)

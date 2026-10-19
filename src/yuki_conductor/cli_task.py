"""`yuki-conductor task …`: the taskboard from a shell or an agent.

Every command goes through the daemon's HTTP API, so the daemon stays the
one writer. Inside a task run `YUKI_TASK_ID` names the current task, so the
id can be omitted, and comments are posted as the agent.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from yuki_conductor.cli_http import api_request


def register(sub) -> argparse.ArgumentParser:
    task = sub.add_parser("task", help="Read and update the taskboard")
    tsub = task.add_subparsers(dest="task_command")

    show = tsub.add_parser("show", help="Print a task and its comment timeline")
    show.add_argument("id", nargs="?", type=int)

    lst = tsub.add_parser("list", help="List tasks")
    lst.add_argument("--status", help="todo, in_progress, needs_input, ready_for_review or done")
    lst.add_argument("--project")
    lst.add_argument("--archived", action="store_true", help="Include archived Done tasks")

    comment = tsub.add_parser("comment", help="Comment on a task")
    _id_and_text(comment)
    comment.add_argument("--quote", type=int, metavar="COMMENT_ID", help="Comment being replied to")
    comment.add_argument("--attach", "-a", action="append", default=[], metavar="PATH")

    ask = tsub.add_parser("ask", help="Ask the user a question; end your turn after")
    _id_and_text(ask)

    pr = tsub.add_parser("pr", help="Register the task's pull request")
    pr.add_argument("args", nargs="+", metavar="[ID] URL")

    create = tsub.add_parser("create", help="Create a task")
    create.add_argument("--project", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--harness")
    create.add_argument("--worktree", action="store_true", help="Work in a fresh git worktree")
    create.add_argument("--attach", "-a", action="append", default=[], metavar="PATH")
    create.add_argument("body", nargs="?", default="", help="Task body (Markdown; - for stdin)")
    return task


def _id_and_text(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("args", nargs="+", metavar="[ID] TEXT", help="TEXT may be - for stdin")


def _env_task_id() -> int | None:
    raw = os.environ.get("YUKI_TASK_ID")
    return int(raw) if raw and raw.isdigit() else None


def _resolve_id(explicit: int | None) -> int:
    task_id = explicit if explicit is not None else _env_task_id()
    if task_id is None:
        _die("No task id: pass one, or run inside a task (YUKI_TASK_ID)")
    return task_id


def _split_id(args: list[str]) -> tuple[int, str]:
    """`[ID] VALUE`: a leading integer is the task id when there are two."""
    if len(args) == 2 and args[0].isdigit():
        return int(args[0]), args[1]
    if len(args) == 1:
        return _resolve_id(None), args[0]
    _die("Expected [ID] and one value (quote multi-word text)")


def _text(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def _files(paths: list[str]) -> list[str]:
    resolved = [str(Path(p).resolve()) for p in paths]
    missing = [p for p in resolved if not Path(p).is_file()]
    if missing:
        _die(f"Not a file: {', '.join(missing)}")
    return resolved


def _die(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def format_task(task: dict) -> str:
    """A task and its timeline as Markdown."""
    lines = [
        f"# Task #{task['id']}: {task['title']}",
        "",
        f"- Status: {task['status_label']}",
        f"- Project: {task['project']} · harness: {task['harness']}"
        + (f" · branch: `{task['branch']}`" if task.get("branch") else "")
        + (" · worktree" if task["use_worktree"] else ""),
    ]
    if task.get("pr_url"):
        state = f" ({task['pr_state'].lower()})" if task.get("pr_state") else ""
        lines.append(f"- PR: {task['pr_url']}{state}")
    if task.get("parent"):
        lines.append(f"- Parent: #{task['parent']['id']} {task['parent']['title']}")
    for child in task.get("children") or []:
        lines.append(f"- Follow-up: #{child['id']} {child['title']} ({child['status']})")
    for att in task["attachments"]:
        lines.append(f"- Attachment: {att['path']}")
    lines += ["", task["body"] or "_(no description)_", "", "## Timeline"]
    for c in task.get("comments") or []:
        who = c["author"]
        if who == "github":
            who = f"github · {c.get('external_author') or 'unknown'}"
        head = f"### #{c['id']} · {who} · {_when(c['created_at'])}"
        if c.get("quote_comment_id"):
            head += f" · replying to #{c['quote_comment_id']}"
        lines += ["", head]
        if c.get("external_url"):
            lines.append(c["external_url"])
        lines += [att["path"] for att in c["attachments"]]
        lines.append(c["body"])
    return "\n".join(lines)


def handle(args) -> None:
    cmd = args.task_command
    if cmd == "show":
        task = api_request("GET", f"/api/tasks/{_resolve_id(args.id)}", what="Show")
        print(format_task(task))
    elif cmd == "list":
        query = "?archived=true" if args.archived else ""
        tasks = api_request("GET", f"/api/tasks{query}", what="List")
        for t in tasks:
            if args.status and t["status"] != args.status:
                continue
            if args.project and t["project"] != args.project:
                continue
            print(f"#{t['id']}\t{t['status_label']}\t{t['project']}\t{t['title']}")
    elif cmd == "comment":
        task_id, text = _split_id(args.args)
        attachments = _files(args.attach)
        text = _text(text)
        if not text.strip() and not attachments:
            _die("Refusing to post an empty comment")
        # On its own task an agent comments as the agent; on any other task
        # its comment is input that task's agent should get, like a user's.
        own = _env_task_id() is not None and task_id == _env_task_id()
        payload = {
            "body": text,
            "author": "agent" if own else "user",
            "quote_comment_id": args.quote,
            "attachments": attachments,
        }
        c = api_request("POST", f"/api/tasks/{task_id}/comments", payload, what="Comment")
        print(f"Posted comment #{c['id']} on task #{task_id}")
    elif cmd == "ask":
        task_id, text = _split_id(args.args)
        if task_id != _env_task_id():
            _die("`task ask` only works on your own running task; use `task comment` elsewhere")
        api_request("POST", f"/api/tasks/{task_id}/ask", {"question": _text(text)}, what="Ask")
        print(f"Asked on task #{task_id}. End your turn now; the user's reply starts the next one.")
    elif cmd == "pr":
        task_id, url = _split_id(args.args)
        api_request("POST", f"/api/tasks/{task_id}/pr", {"url": url}, what="Register PR")
        print(f"Registered {url} on task #{task_id}")
    elif cmd == "create":
        payload = {
            "title": args.title,
            "project": args.project,
            "harness": args.harness,
            "use_worktree": args.worktree,
            "body": _text(args.body),
            "attachments": _files(args.attach),
            "parent_task_id": _env_task_id(),
        }
        t = api_request("POST", "/api/tasks", payload, what="Create")
        print(f"Created task #{t['id']}: {t['title']}")
    else:
        _die("usage: yuki-conductor task {show,list,comment,ask,pr,create} ...")

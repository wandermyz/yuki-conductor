"""`yuki-conductor search …` and `conversation show`: find past work, then read it.

Both go through the daemon's HTTP API, the same search the web UI runs. Every
hit carries a link into the web UI (`WEB_PUBLIC_URL`) so an agent can hand
the user something to click.
"""

import argparse
import sys
from datetime import datetime
from urllib.parse import quote

from yuki_conductor.cli_http import api_request
from yuki_conductor.conversation_store import SNIPPET_MARK


def register(sub) -> None:
    search = sub.add_parser("search", help="Keyword search over conversations and tasks")
    search.add_argument("query", nargs="+", help="Terms; each is a prefix match, all must match")
    kind = search.add_mutually_exclusive_group()
    kind.add_argument("--conversations", action="store_true", help="Only conversations")
    kind.add_argument("--tasks", action="store_true", help="Only tasks")
    search.add_argument("--limit", "-n", type=int, default=10, help="Hits per kind (default 10)")

    conv = sub.add_parser("conversation", help="Read web conversations")
    csub = conv.add_subparsers(dest="conversation_command")
    show = csub.add_parser("show", help="Print a conversation's messages")
    show.add_argument("id")
    show.add_argument(
        "--limit", "-n", type=int, default=100, help="Most recent messages (default 100)"
    )


def link(fragment: str) -> str:
    """A web UI URL for a hash route like `chat/<id>` or `tasks/<id>`."""
    from yuki_conductor.web_server import WEB_PUBLIC_URL

    return f"{WEB_PUBLIC_URL.rstrip('/')}/#{fragment}"


def _snippet(text: str) -> str:
    start, end = SNIPPET_MARK
    return " ".join(text.replace(start, "**").replace(end, "**").split())


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def format_conversation_hits(hits: list[dict]) -> str:
    lines = [f"## Conversations ({len(hits)})"]
    for h in hits:
        lines += [
            "",
            f"- {h['title'] or '(untitled)'} · `{h['conversation_id']}`",
            f"  {_snippet(h['snippet'])}",
            f"  {link('chat/' + h['conversation_id'])}",
        ]
    return "\n".join(lines)


def format_task_hits(hits: list[dict]) -> str:
    lines = [f"## Tasks ({len(hits)})"]
    for h in hits:
        where = f" · comment #{h['comment_id']}" if h["comment_id"] else ""
        lines += [
            "",
            f"- #{h['task_id']} {h['title']} · {h['status_label']}{where}",
            f"  {_snippet(h['snippet'])}",
            f"  {link('tasks/' + str(h['task_id']))}",
        ]
    return "\n".join(lines)


def format_conversation(conv: dict, messages: list[dict]) -> str:
    """A conversation and its messages as Markdown."""
    lines = [
        f"# {conv['title'] or '(untitled)'}",
        "",
        f"- Id: `{conv['id']}` · harness: {conv['harness']}"
        + (f" · project: {conv['project']}" if conv.get("project") else ""),
        f"- Started {_when(conv['created_at'])}, last active {_when(conv['updated_at'])}",
        f"- {link('chat/' + conv['id'])}",
    ]
    for m in messages:
        lines += ["", f"### {m['role']} · {_when(m['created_at'])}"]
        lines += [a["filename"] for a in m["attachments"]]
        lines.append(m["text"])
    return "\n".join(lines)


def handle_search(args: argparse.Namespace) -> None:
    q = quote(" ".join(args.query))
    sections = []
    if not args.tasks:
        hits = api_request("GET", f"/api/search?q={q}&limit={args.limit}", what="Search")
        sections.append(format_conversation_hits(hits))
    if not args.conversations:
        hits = api_request("GET", f"/api/tasks/search?q={q}&limit={args.limit}", what="Search")
        sections.append(format_task_hits(hits))
    print("\n\n".join(sections))


def handle_conversation(args: argparse.Namespace) -> None:
    if args.conversation_command != "show":
        print("usage: yuki-conductor conversation show ID", file=sys.stderr)
        sys.exit(1)
    cid = quote(args.id, safe="")
    conv = api_request("GET", f"/api/conversations/{cid}", what="Show")
    messages = api_request(
        "GET", f"/api/conversations/{cid}/messages?limit={args.limit}", what="Show"
    )
    print(format_conversation(conv, messages))

"""`yuki-conductor heartbeat …`: recurring wake-ups of this conversation or task.

Like `task` and `send`, every command goes through the daemon's HTTP API.
The target defaults to the run's own: `YUKI_TASK_ID` in a task turn,
otherwise `YUKI_CONVERSATION_ID`. `-c` / `--task` name another.
"""

import argparse
import os
import sys
from datetime import datetime

from yuki_conductor.cli_http import api_request
from yuki_conductor.heartbeat import format_duration, parse_duration
from yuki_conductor.heartbeat_store import DEFAULT_INTERVAL_S

# Intervals in this range miss Anthropic's 5-minute prompt cache on every
# tick, yet tick often enough for that to add up.
_CACHE_COLD_WARNING = (5 * 60, 30 * 60)


def register(sub) -> argparse.ArgumentParser:
    hb = sub.add_parser(
        "heartbeat", help="Wake this conversation or task on an interval, resuming its session"
    )
    hsub = hb.add_subparsers(dest="heartbeat_command")

    set_ = hsub.add_parser("set", help="Set (or replace) the heartbeat")
    set_.add_argument(
        "--every", default=f"{DEFAULT_INTERVAL_S}s",
        help="Interval, e.g. 270s, 60m, 1h (default 270s, min 60s)",
    )
    set_.add_argument("--for", dest="lifetime", default="7d", help="Lifetime, at most 7d")
    _target_args(set_)
    set_.add_argument("prompt", help="What each tick should check and do (- for stdin)")

    for name, help_ in (
        ("status", "Show the heartbeat and its recent ticks"),
        ("pause", "Pause the heartbeat"),
        ("resume", "Resume a paused heartbeat"),
        ("stop", "Stop and delete the heartbeat"),
    ):
        _target_args(hsub.add_parser(name, help=help_))
    return hb


def _target_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-c", "--conversation", help="Conversation id (default: this run's)")
    group.add_argument("--task", type=int, help="Task id (default: this run's)")


def _die(message: str):
    print(message, file=sys.stderr)
    sys.exit(1)


def _target(args) -> tuple[str, str]:
    if args.task is not None:
        return "task", str(args.task)
    if args.conversation:
        return "conversation", args.conversation
    task_id = os.environ.get("YUKI_TASK_ID")
    if task_id:
        return "task", task_id
    conv = os.environ.get("YUKI_CONVERSATION_ID")
    if conv:
        return "conversation", conv
    _die("No target: pass -c CONVERSATION or --task ID, or run inside a conversation or task")


def _name(kind: str, target_id: str) -> str:
    return f"task #{target_id}" if kind == "task" else f"conversation {target_id}"


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def format_status(hb: dict | None, ticks: list[dict]) -> str:
    if hb is None:
        return "No heartbeat."
    state = "paused" if hb["paused"] else "active"
    lines = [
        f"Heartbeat ({state}), every {format_duration(hb['interval_s'])}, "
        f"set by {hb['created_by']} at {_when(hb['created_at'])}",
        f"- Next tick: {_when(hb['next_due_at'])}" if not hb["paused"] else "- Paused",
        f"- Expires: {_when(hb['expires_at'])}",
        f"- Prompt: {hb['prompt']}",
    ]
    if hb["last_outcome"]:
        lines.append(f"- Last tick: {_when(hb['last_tick_at'])}, {hb['last_outcome']}")
    if ticks:
        lines += ["", "Recent ticks:"]
        for t in ticks[:10]:
            outcome = t["outcome"] or "running"
            detail = t["error"] or t["response"] or ""
            detail = detail.strip().splitlines()[0][:100] if detail.strip() else ""
            lines.append(f"- {_when(t['started_at'])} · {outcome}" + (f" · {detail}" if detail else ""))
    return "\n".join(lines)


def handle(args) -> None:
    cmd = args.heartbeat_command
    if cmd is None:
        _die("usage: yuki-conductor heartbeat {set,status,pause,resume,stop} ...")
    kind, target_id = _target(args)
    path = f"/api/heartbeats/{kind}/{target_id}"
    name = _name(kind, target_id)
    if cmd == "set":
        try:
            interval = parse_duration(args.every)
            lifetime = parse_duration(args.lifetime)
        except ValueError as exc:
            _die(str(exc))
        prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
        low, high = _CACHE_COLD_WARNING
        if low < interval < high:
            print(
                f"warning: every {format_duration(interval)} misses the 5-minute prompt "
                "cache on each tick; 270s or 30m+ usually costs less.",
                file=sys.stderr,
            )
        payload = {
            "prompt": prompt, "interval_s": interval, "lifetime_s": lifetime,
            "created_by": "agent",
        }
        hb = api_request("PUT", path, payload, what="Set heartbeat")
        print(
            f"Heartbeat set on {name}: every {format_duration(hb['interval_s'])} "
            f"until {_when(hb['expires_at'])}."
        )
    elif cmd == "status":
        data = api_request("GET", path, what="Status")
        print(format_status(data["heartbeat"], data["ticks"]))
    elif cmd in ("pause", "resume"):
        api_request("PATCH", path, {"paused": cmd == "pause"}, what=cmd.capitalize())
        print(f"Heartbeat {'paused' if cmd == 'pause' else 'resumed'} on {name}.")
    elif cmd == "stop":
        api_request("DELETE", path, what="Stop")
        print(f"Heartbeat stopped on {name}.")

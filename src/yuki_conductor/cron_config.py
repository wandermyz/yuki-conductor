"""Reading, describing, and surgically editing ``workspace/cron.yaml``.

The scheduler only ever *reads* this file, but the Automations UI also renames
tasks. Rewriting the whole document with PyYAML would reflow every folded
``prompt: >`` block and drop comments, so edits here are line-based: find the
task's block by its ``name:`` key and set one scalar field inside it, leaving
every other byte untouched.
"""

import re
from dataclasses import dataclass

import yaml
from croniter import croniter

from yuki_conductor.config import CRON_FILE

# Fields the UI is allowed to write back into cron.yaml.
EDITABLE_FIELDS = frozenset({"display_name"})

_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


@dataclass
class CronTask:
    name: str
    schedule: str
    description: str
    prompt: str
    chat_app: str | None = None  # "slack" | plugin name | None (default routing)
    display_name: str | None = None  # user-facing label, renameable from the UI
    origin_conversation: str | None = None  # web conversation that created the task

    @property
    def label(self) -> str:
        """What the sidebar shows: explicit name, else description, else prompt."""
        if self.display_name:
            return self.display_name
        if self.description:
            return self.description
        return truncate_prompt(self.prompt)


def truncate_prompt(prompt: str, limit: int = 60) -> str:
    """Collapse a prompt to a one-line label."""
    flat = " ".join(prompt.split())
    if len(flat) <= limit:
        return flat or "(empty prompt)"
    return flat[: limit - 1].rstrip() + "…"


def _describe_field(
    field: str, names: list[str] | None, singular: str, plural: str
) -> str:
    """Render one cron field as prose, or "" when it's the unrestricted ``*``."""
    if field == "*":
        return ""

    def render(token: str) -> str:
        def name(v: str) -> str:
            if names is None:
                return v
            try:
                return names[int(v)]
            except (ValueError, IndexError):
                return v

        if "-" in token:
            lo, _, hi = token.partition("-")
            return f"{name(lo)} through {name(hi)}"
        return name(token)

    step = None
    base = field
    if "/" in field:
        base, _, step = field.partition("/")

    if step:
        if base in ("*", "0"):
            return f"every {step} {plural}"
        return f"every {step} {plural} within {', '.join(render(t) for t in base.split(','))}"

    parts = [render(t) for t in base.split(",")]
    joined = ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else parts[0]
    return f"{singular} {joined}" if names is None else joined


def _all_digits(field: str) -> bool:
    """True for a plain hour list like ``9`` or ``9,17``."""
    return all(part.isdigit() for part in field.split(","))


def _is_hour_range(field: str) -> bool:
    """True for a simple ``lo-hi`` range with no step or list."""
    lo, sep, hi = field.partition("-")
    return bool(sep) and lo.isdigit() and hi.isdigit()


def describe_schedule(expr: str) -> str:
    """Turn a 5-field cron expression into a plain-English cadence."""
    fields = expr.split()
    if len(fields) != 5:
        return expr
    minute, hour, dom, month, dow = fields

    # Fixed time-of-day is by far the common case, so name it directly.
    when = ""
    if minute.isdigit() and hour.isdigit():
        when = f"at {int(hour):02d}:{int(minute):02d}"
    elif hour == "*" and minute.isdigit():
        when = f"at :{int(minute):02d} past every hour"
    elif minute.startswith("*/") and hour == "*":
        when = f"every {minute[2:]} minutes"
    elif minute.isdigit() and _all_digits(hour):
        # An explicit set of hours at a fixed minute: "at 09:00 and 17:00".
        times = [f"{int(h):02d}:{int(minute):02d}" for h in hour.split(",")]
        when = "at " + (", ".join(times[:-1]) + f" and {times[-1]}" if len(times) > 1 else times[0])
    elif minute.isdigit() and _is_hour_range(hour):
        # A fixed minute across an hour range: "hourly from 09:17 to 23:17".
        lo, _, hi = hour.partition("-")
        when = f"hourly from {int(lo):02d}:{int(minute):02d} to {int(hi):02d}:{int(minute):02d}"
    elif minute.isdigit():
        when = (
            f"at :{int(minute):02d} past "
            f"{_describe_field(hour, None, 'hour', 'hours')}"
        )
    else:
        bits = [
            _describe_field(hour, None, "hour", "hours"),
            _describe_field(minute, None, "minute", "minutes"),
        ]
        when = ", ".join(b for b in bits if b) or "every minute"

    parts = [when]
    dow_text = _describe_field(dow, _DAYS, "day", "days")
    dom_text = _describe_field(dom, None, "day-of-month", "days")
    month_text = _describe_field(month, _MONTHS, "month", "months")

    if dow_text:
        parts.append(f"on {dow_text}")
    if dom_text:
        parts.append(f"on {dom_text}")
    if month_text:
        parts.append(f"in {month_text}")
    if not dow_text and not dom_text and not month_text:
        parts.append("every day")

    return " ".join(parts)


def next_fire_times(expr: str, count: int = 3, base=None) -> list[float]:
    """Upcoming fire times as epoch seconds, or [] for an invalid expression."""
    from datetime import datetime

    try:
        it = croniter(expr, base or datetime.now())
        return [it.get_next(datetime).timestamp() for _ in range(count)]
    except (ValueError, KeyError):
        return []


def load_tasks(path=None) -> list[CronTask]:
    """Parse every valid task out of cron.yaml. Invalid entries are skipped."""
    path = path or CRON_FILE
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "tasks" not in data:
        return []

    tasks = []
    for entry in data["tasks"] or []:
        try:
            task = CronTask(
                name=entry["name"],
                schedule=entry["schedule"],
                description=entry.get("description", ""),
                prompt=entry["prompt"],
                chat_app=entry.get("chat_app"),
                display_name=entry.get("display_name"),
                origin_conversation=entry.get("origin_conversation"),
            )
            croniter(task.schedule)
        except (KeyError, ValueError, TypeError):
            continue
        tasks.append(task)
    return tasks


def _yaml_scalar(value: str) -> str:
    """Quote a value so it survives a round trip as a YAML scalar."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_task_field(task_name: str, field: str, value: str | None, path=None) -> bool:
    """Set one scalar ``field`` on the task named ``task_name``.

    Rewrites only the affected lines, so folded prompts and comments elsewhere
    in the file survive verbatim. Returns False if the task isn't found.
    """
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"Field {field!r} is not editable")

    path = path or CRON_FILE
    if not path.exists():
        return False
    lines = open(path, encoding="utf-8").read().splitlines(keepends=True)

    # Locate the `- name: <task_name>` line that opens the task's block.
    start = None
    indent = ""
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-(\s+)name:\s*(.+?)\s*$", line)
        if m and m.group(3).strip("\"'") == task_name:
            start = i
            # Keys inside the block align with `name:`, past the "- ".
            indent = m.group(1) + " " * (1 + len(m.group(2)))
            break
    if start is None:
        return False

    # The block ends at the next list item at or above the dash's indent.
    dash_indent = len(indent) - 2
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(lines[i]) - len(stripped)
        if lead <= dash_indent and (stripped.startswith("- ") or lead < dash_indent):
            end = i
            break

    key_re = re.compile(rf"^{re.escape(indent)}{re.escape(field)}:\s")
    existing = next((i for i in range(start, end) if key_re.match(lines[i])), None)

    if value is None:
        if existing is not None:
            del lines[existing]
    else:
        new_line = f"{indent}{field}: {_yaml_scalar(value)}\n"
        if existing is not None:
            lines[existing] = new_line
        else:
            lines.insert(start + 1, new_line)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True

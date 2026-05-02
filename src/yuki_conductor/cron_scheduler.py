"""Cron scheduler that reads tasks from workspace YAML and triggers Claude runs."""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

import yaml
from croniter import croniter

from yuki_conductor.claude_runner import run_claude
from yuki_conductor.config import CRON_FILE, WORKSPACE_DIR
from yuki_conductor.messaging import MessagingPlatform

logger = logging.getLogger(__name__)

# Preference order when a cron task does not specify chat_app and multiple
# platforms are enabled.
_PREFERRED_ORDER = ("slack", "teams_cli", "web")


@dataclass
class CronTask:
    name: str
    schedule: str
    description: str
    prompt: str
    chat_app: str | None = None  # "slack" | "teams_cli" | None (default routing)


def _ensure_workspace() -> None:
    """Create the workspace directory if it doesn't exist."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Workspace directory ensured at {WORKSPACE_DIR}")


def _load_cron_tasks() -> list[CronTask]:
    """Load cron tasks from the workspace YAML file."""
    if not CRON_FILE.exists():
        logger.info(f"No cron file found at {CRON_FILE}, skipping cron scheduling")
        return []

    with open(CRON_FILE) as f:
        data = yaml.safe_load(f)

    if not data or "tasks" not in data:
        logger.warning(f"Cron file {CRON_FILE} has no 'tasks' key")
        return []

    tasks = []
    for entry in data["tasks"]:
        try:
            task = CronTask(
                name=entry["name"],
                schedule=entry["schedule"],
                description=entry.get("description", ""),
                prompt=entry["prompt"],
                chat_app=entry.get("chat_app"),
            )
            # Validate cron expression
            croniter(task.schedule)
            tasks.append(task)
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid cron task entry: {entry} — {e}")

    logger.info(f"Loaded {len(tasks)} cron task(s) from {CRON_FILE}")
    return tasks


_CRON_PROMPT_PREFIX = (
    "You are running as a scheduled cron task. After completing your work, "
    "decide whether the user needs to be notified.\n"
    "- If the user should be notified (e.g. a reminder they need to act on, "
    "an important result, or an error), include <notify> at the very end of "
    "your response.\n"
    "- If no notification is needed (e.g. the task is already done, nothing "
    "changed, or it's a silent check), include <silence> at the very end of "
    "your response.\n\n"
    "Now here is your task:\n"
)


# `slack` (legacy) is accepted as an alias for `slack_socket` since
# CHAT_APPS uses the latter while platform.name is the former.
_TASK_APP_ALIASES = {"slack_socket": "slack"}


def _pick_platform(
    task: CronTask,
    platforms_by_name: dict[str, MessagingPlatform],
) -> MessagingPlatform | None:
    """Resolve which platform a cron task should notify into.

    Returns None when the task should be skipped (explicit chat_app refers
    to a disabled platform) or when no platforms are enabled at all.
    """
    if task.chat_app:
        requested = _TASK_APP_ALIASES.get(task.chat_app, task.chat_app)
        platform = platforms_by_name.get(requested)
        if platform is None:
            logger.warning(
                f"Cron task={task.name} requested chat_app={task.chat_app!r} "
                f"but it is not enabled; skipping notification"
            )
        return platform

    for name in _PREFERRED_ORDER:
        if name in platforms_by_name:
            return platforms_by_name[name]
    return None


def _run_cron_task(
    task: CronTask, platforms_by_name: dict[str, MessagingPlatform]
) -> None:
    """Execute a single cron task: run Claude, post via the routed platform if <notify>."""
    logger.info(f"Cron task={task.name} starting, running Claude...")

    prefixed_prompt = _CRON_PROMPT_PREFIX + task.prompt
    result = run_claude(prefixed_prompt)

    response_text = result.text or ""
    should_notify = "<notify>" in response_text
    display_text = response_text.replace("<notify>", "").replace("<silence>", "").strip()

    if not should_notify:
        logger.info(f"Cron task={task.name} completed silently: {display_text[:200]}")
        return

    logger.info(f"Cron task={task.name} completed with notification")

    if not platforms_by_name:
        logger.info(f"Cron task={task.name} notification (no chat apps enabled): {display_text}")
        return

    platform = _pick_platform(task, platforms_by_name)
    if platform is None:
        return

    title = task.description or task.name
    try:
        conversation_key = platform.start_thread(display_text, title=title)
    except Exception:
        logger.error(
            f"Failed to start cron thread on platform={platform.name} for task={task.name}",
            exc_info=True,
        )
        return

    if result.session_id:
        platform.set_session_id(conversation_key, result.session_id, title_hint=title)


def _build_task_state(tasks: list[CronTask]) -> tuple[list[tuple[CronTask, croniter]], dict[str, datetime]]:
    """Build croniter instances and next-fire-time map from a task list."""
    iters = [(task, croniter(task.schedule, datetime.now())) for task in tasks]
    next_times = {task.name: it.get_next(datetime) for task, it in iters}
    return iters, next_times


def _tasks_changed(old: list[CronTask], new: list[CronTask]) -> bool:
    """Check if the task list has changed (by comparing as tuples)."""
    def to_tuple(t: CronTask) -> tuple:
        return (t.name, t.schedule, t.description, t.prompt, t.chat_app)
    return [to_tuple(t) for t in old] != [to_tuple(t) for t in new]


def _scheduler_loop(
    platforms_by_name: dict[str, MessagingPlatform],
    stop_event: threading.Event,
) -> None:
    """Main loop that reloads cron file every cycle and fires due tasks."""
    current_tasks: list[CronTask] = []
    iters: list[tuple[CronTask, croniter]] = []
    next_times: dict[str, datetime] = {}

    while not stop_event.is_set():
        new_tasks = _load_cron_tasks()
        if _tasks_changed(current_tasks, new_tasks):
            current_tasks = new_tasks
            if current_tasks:
                iters, next_times = _build_task_state(current_tasks)
                logger.info(f"Cron tasks reloaded: {[t.name for t in current_tasks]}")
            else:
                iters, next_times = [], {}
                logger.info("Cron tasks cleared")

        now = datetime.now()
        for task, it in iters:
            fire_at = next_times.get(task.name)
            if fire_at and now >= fire_at:
                logger.info(f"Cron firing task={task.name} (scheduled={fire_at})")
                threading.Thread(
                    target=_run_cron_task,
                    args=(task, platforms_by_name),
                    name=f"cron-{task.name}",
                    daemon=True,
                ).start()
                next_times[task.name] = it.get_next(datetime)

        stop_event.wait(timeout=30)


def start_cron_scheduler(
    platforms_by_name: dict[str, MessagingPlatform] | None = None,
) -> threading.Event:
    """Initialize workspace and start the cron scheduler thread.

    The scheduler reloads workspace/cron.yaml every 30 seconds, so changes
    take effect without restarting the daemon.

    `platforms_by_name` maps a platform name (e.g. "slack", "teams_cli") to
    its `MessagingPlatform`. Pass an empty dict / None to run cron without
    any chat-app delivery (notifications are logged instead).

    Returns a stop_event that can be set to stop the scheduler.
    """
    _ensure_workspace()
    platforms_by_name = platforms_by_name or {}

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_scheduler_loop,
        args=(platforms_by_name, stop_event),
        name="cron-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info(
        f"Cron scheduler started (chat apps: {sorted(platforms_by_name.keys()) or 'none'})"
    )
    return stop_event

"""Cron scheduler that reads tasks from workspace YAML and triggers Claude runs."""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

import yaml
from croniter import croniter

from yuki_conductor.claude_runner import run_claude
from yuki_conductor.config import CRON_FILE, WORKSPACE_DIR, slack_cron_channel
from yuki_conductor.formatting import markdown_to_mrkdwn
from yuki_conductor.store import SessionStore

logger = logging.getLogger(__name__)

store = SessionStore()


@dataclass
class CronTask:
    name: str
    schedule: str
    description: str
    prompt: str


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


def _run_cron_task(task: CronTask, slack_client) -> None:
    """Execute a single cron task: run Claude, post to Slack only if <notify>.

    When `slack_client` is None (Slack disabled), notifications are logged
    instead of posted.
    """
    logger.info(f"Cron task={task.name} starting, running Claude...")

    # Run Claude with the cron prompt, prefixed with notify/silence instructions
    prefixed_prompt = _CRON_PROMPT_PREFIX + task.prompt
    result = run_claude(prefixed_prompt)

    # Determine whether to notify the user
    response_text = result.text or ""
    should_notify = "<notify>" in response_text

    # Strip the notify/silence tags from the displayed text
    display_text = response_text.replace("<notify>", "").replace("<silence>", "").strip()

    if not should_notify:
        logger.info(f"Cron task={task.name} completed silently: {display_text[:200]}")
        return

    logger.info(f"Cron task={task.name} completed with notification")

    if slack_client is None:
        logger.info(f"Cron task={task.name} notification (Slack disabled): {display_text}")
        return

    # Post Claude's response directly to Slack
    channel = slack_cron_channel()
    display_text = markdown_to_mrkdwn(display_text)
    try:
        response = slack_client.chat_postMessage(channel=channel, text=display_text)
        thread_ts = response["ts"]
    except Exception:
        logger.error(f"Failed to post cron message for task={task.name}", exc_info=True)
        return

    # Store session for potential follow-up in the thread
    if result.session_id:
        store.set(
            thread_ts,
            result.session_id,
            channel_id=channel,
            title=task.description or task.name,
        )


def _build_task_state(tasks: list[CronTask]) -> tuple[list[tuple[CronTask, croniter]], dict[str, datetime]]:
    """Build croniter instances and next-fire-time map from a task list."""
    iters = [(task, croniter(task.schedule, datetime.now())) for task in tasks]
    next_times = {task.name: it.get_next(datetime) for task, it in iters}
    return iters, next_times


def _tasks_changed(old: list[CronTask], new: list[CronTask]) -> bool:
    """Check if the task list has changed (by comparing as tuples)."""
    def to_tuple(t: CronTask) -> tuple[str, str, str, str]:
        return (t.name, t.schedule, t.description, t.prompt)
    return [to_tuple(t) for t in old] != [to_tuple(t) for t in new]


def _scheduler_loop(slack_client, stop_event: threading.Event) -> None:
    """Main loop that reloads cron file every cycle and fires due tasks."""
    current_tasks: list[CronTask] = []
    iters: list[tuple[CronTask, croniter]] = []
    next_times: dict[str, datetime] = {}

    while not stop_event.is_set():
        # Reload cron file and rebuild state if changed
        new_tasks = _load_cron_tasks()
        if _tasks_changed(current_tasks, new_tasks):
            current_tasks = new_tasks
            if current_tasks:
                iters, next_times = _build_task_state(current_tasks)
                logger.info(f"Cron tasks reloaded: {[t.name for t in current_tasks]}")
            else:
                iters, next_times = [], {}
                logger.info("Cron tasks cleared")

        # Check and fire due tasks
        now = datetime.now()
        for task, it in iters:
            fire_at = next_times.get(task.name)
            if fire_at and now >= fire_at:
                logger.info(f"Cron firing task={task.name} (scheduled={fire_at})")
                threading.Thread(
                    target=_run_cron_task,
                    args=(task, slack_client),
                    name=f"cron-{task.name}",
                    daemon=True,
                ).start()
                next_times[task.name] = it.get_next(datetime)

        stop_event.wait(timeout=30)


def start_cron_scheduler(slack_client=None) -> threading.Event:
    """Initialize workspace and start the cron scheduler thread.

    The scheduler reloads workspace/cron.yaml every 30 seconds,
    so changes take effect without restarting the daemon.

    Pass `slack_client=None` to run cron tasks without Slack delivery
    (notifications get logged instead).

    Returns a stop_event that can be set to stop the scheduler.
    """
    _ensure_workspace()

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_scheduler_loop,
        args=(slack_client, stop_event),
        name="cron-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info("Cron scheduler started")
    return stop_event

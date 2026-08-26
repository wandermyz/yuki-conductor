"""Cron scheduler that reads tasks from workspace YAML and triggers Claude runs."""

import logging
import threading
from datetime import datetime

from croniter import croniter

from yuki_conductor.claude_runner import run_claude
from yuki_conductor.config import WORKSPACE_DIR, chat_apps
from yuki_conductor.cron_config import CronTask, load_tasks
from yuki_conductor.messaging import MessagingPlatform, OutgoingMessage
from yuki_conductor.store import CronRunStore

logger = logging.getLogger(__name__)

run_store = CronRunStore()

# Platforms registered by runtime.start(), so a manually triggered run from the
# web UI notifies the same place a scheduled firing would.
_platforms: dict[str, MessagingPlatform] = {}


def registered_platforms() -> dict[str, MessagingPlatform]:
    return _platforms


def _ensure_workspace() -> None:
    """Create the workspace directory if it doesn't exist."""
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Workspace directory ensured at {WORKSPACE_DIR}")


def _load_cron_tasks() -> list[CronTask]:
    """Load cron tasks from the workspace YAML file."""
    tasks = load_tasks()
    logger.info(f"Loaded {len(tasks)} cron task(s)")
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

    # Default: derive preference from CHAT_APPS order + "web" fallback
    preferred = [_TASK_APP_ALIASES.get(a, a) for a in chat_apps()] + ["web"]
    for name in preferred:
        if name in platforms_by_name:
            return platforms_by_name[name]
    return None


def _run_cron_task(
    task: CronTask,
    platforms_by_name: dict[str, MessagingPlatform],
    trigger: str = "schedule",
) -> None:
    """Execute a single cron task: run Claude, post via the routed platform if <notify>.

    Every firing is recorded in ``cron_runs`` — a 'running' row up front so the
    Automations tab can show a run in flight, updated with the response (or the
    traceback, if the run blew up) when it settles.
    """
    logger.info(f"Cron task={task.name} starting ({trigger}), running Claude...")
    run_id = run_store.start_run(task.name, trigger=trigger)

    try:
        _execute(task, platforms_by_name, run_id)
    except Exception as exc:
        logger.error(f"Cron task={task.name} failed", exc_info=True)
        run_store.finish_run(
            run_id, status="error", error=f"{type(exc).__name__}: {exc}"
        )


def _execute(
    task: CronTask,
    platforms_by_name: dict[str, MessagingPlatform],
    run_id: int,
) -> None:
    title = task.label
    platform = _pick_platform(task, platforms_by_name) if platforms_by_name else None

    # Reserve the destination conversation up front where the platform supports
    # it, so the run itself can push interim messages into the same thread via
    # `yuki-conductor send` instead of only speaking through its final answer.
    reserved: str | None = None
    if platform is not None and hasattr(platform, "reserve_thread"):
        try:
            reserved = platform.reserve_thread(title=title)
        except Exception:
            logger.error(
                f"Failed to reserve thread on platform={platform.name} for task={task.name}",
                exc_info=True,
            )

    prefixed_prompt = _CRON_PROMPT_PREFIX + task.prompt
    result = run_claude(prefixed_prompt, web_conversation_id=reserved)

    response_text = result.text or ""
    should_notify = "<notify>" in response_text
    display_text = response_text.replace("<notify>", "").replace("<silence>", "").strip()

    if result.is_error:
        run_store.finish_run(
            run_id,
            status="error",
            error=display_text or "Claude run failed with no output",
            session_id=result.session_id,
            conversation_id=reserved,
        )
    else:
        run_store.finish_run(
            run_id,
            status="success",
            response=display_text,
            notified=should_notify,
            session_id=result.session_id,
            conversation_id=reserved,
        )

    if not should_notify:
        logger.info(f"Cron task={task.name} completed silently: {display_text[:200]}")
        if reserved is not None and platform is not None:
            # Keep the thread only if the run actually posted into it.
            platform.discard_thread(reserved)
        return

    logger.info(f"Cron task={task.name} completed with notification")

    if not platforms_by_name:
        logger.info(f"Cron task={task.name} notification (no chat apps enabled): {display_text}")
        return

    if platform is None:
        return

    try:
        if reserved is not None:
            platform.send(reserved, OutgoingMessage(text=display_text))
            conversation_key = reserved
        else:
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
    """Build croniter instances and next-fire-time map from a task list.

    Paused tasks are left out entirely, so they hold no schedule state at all.
    """
    iters = [
        (task, croniter(task.schedule, datetime.now()))
        for task in tasks
        if not task.paused
    ]
    next_times = {task.name: it.get_next(datetime) for task, it in iters}
    return iters, next_times


def _tasks_changed(old: list[CronTask], new: list[CronTask]) -> bool:
    """Check if the task list has changed (by comparing as tuples)."""
    def to_tuple(t: CronTask) -> tuple:
        return (t.name, t.schedule, t.description, t.prompt, t.chat_app, t.paused)
    return [to_tuple(t) for t in old] != [to_tuple(t) for t in new]


def trigger_task(task_name: str) -> bool:
    """Fire a task right now, off-schedule. Returns False if it isn't defined.

    Backs the "Run now" button in the Automations tab. The run goes through the
    same path as a scheduled firing — same prompt prefix, same notification
    routing — and is tagged ``trigger='manual'`` in the history.
    """
    task = next((t for t in _load_cron_tasks() if t.name == task_name), None)
    if task is None:
        return False
    threading.Thread(
        target=_run_cron_task,
        args=(task, _platforms, "manual"),
        name=f"cron-manual-{task.name}",
        daemon=True,
    ).start()
    return True


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
                paused = [t.name for t in current_tasks if t.paused]
                logger.info(
                    f"Cron tasks reloaded: {[t.name for t, _ in iters]}"
                    + (f" (paused: {paused})" if paused else "")
                )
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

    The scheduler reloads ~/.yuki-conductor/workspace/cron.yaml every 30 seconds, so changes
    take effect without restarting the daemon.

    `platforms_by_name` maps a platform name (e.g. "slack") to
    its `MessagingPlatform`. Pass an empty dict / None to run cron without
    any chat-app delivery (notifications are logged instead).

    Returns a stop_event that can be set to stop the scheduler.
    """
    _ensure_workspace()
    platforms_by_name = platforms_by_name or {}
    _platforms.clear()
    _platforms.update(platforms_by_name)

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

"""Heartbeats: recurring wake-ups that resume a conversation's or task's session.

Cron runs a fresh, isolated session each time. A heartbeat instead belongs to
one target (a web or Slack conversation, or a taskboard task) and each tick
resumes that target's own session with the heartbeat's prompt, so the agent
carries on with all its context. Nothing reaches the user unless the reply
contains <notify>; quiet ticks are recorded only in `heartbeat_ticks`.

This module holds what the API and CLI call (`set_heartbeat`, `update`,
`stop`, …), the lifecycle hooks, and the scheduler loop. The loop runs
conversation ticks itself; task ticks go through the taskboard harvester
(`due_tasks`), which keeps its concurrency limits and pause switch in force.
See docs/plans/2026-11-10-heartbeat.md.
"""

import logging
import random
import re
import threading
from datetime import datetime

from yuki_conductor import heartbeat_store as hs
from yuki_conductor.heartbeat_store import (
    CONVERSATION,
    DEFAULT_INTERVAL_S,
    ERROR,
    MAX_LIFETIME_S,
    MIN_INTERVAL_S,
    NOTIFIED,
    QUIET,
    TARGET_KINDS,
    TASK,
    Heartbeat,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = 15.0
# Overdue heartbeats fire within this many seconds of a restart, spread out.
STARTUP_JITTER_S = 60
# Errors in a row before a heartbeat pauses itself.
MAX_CONSECUTIVE_ERRORS = 3


class HeartbeatError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# -- durations ---------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """`270`, `270s`, `60m`, `1h`, `3d` → seconds. Raises ValueError."""
    match = _DURATION_RE.match(text.lower())
    if not match:
        raise ValueError(f"Not a duration: {text!r} (use e.g. 270s, 60m, 1h, 3d)")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def format_duration(seconds: float) -> str:
    """270 → `4m30s`, 3600 → `1h`, 90000 → `1d1h`."""
    seconds = int(round(seconds))
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts) or "0s"


# -- what the API and CLI call -------------------------------------------------

_manager = None  # the web ConnectionManager, once `start` has run


def _check_kind(kind: str) -> None:
    if kind not in TARGET_KINDS:
        raise HeartbeatError(404, f"Unknown heartbeat target kind {kind!r}")


def _check_target(kind: str, target_id: str) -> None:
    """The target exists and can take a heartbeat."""
    if kind == TASK:
        from yuki_conductor import taskboard
        from yuki_conductor.task_store import DONE

        task = taskboard.store.get(int(target_id)) if target_id.isdigit() else None
        if task is None or task.archived_at is not None:
            raise HeartbeatError(404, f"Task #{target_id} not found")
        if task.status == DONE:
            raise HeartbeatError(409, "A Done task can't have a heartbeat")
    elif _resolve_conversation(target_id) is None:
        raise HeartbeatError(404, f"Conversation {target_id!r} not found")


def validate(prompt: str, interval_s: int) -> None:
    """Raise HeartbeatError unless these could make a heartbeat."""
    if not prompt.strip():
        raise HeartbeatError(400, "A heartbeat needs a prompt")
    _check_interval(interval_s)


def _check_interval(interval_s: int) -> None:
    if interval_s < MIN_INTERVAL_S:
        raise HeartbeatError(400, f"The interval must be at least {MIN_INTERVAL_S}s")
    if interval_s > MAX_LIFETIME_S:
        raise HeartbeatError(400, "The interval can't be longer than 7 days")


def get(kind: str, target_id: str) -> Heartbeat | None:
    _check_kind(kind)
    return hs.store.get(kind, str(target_id))


def set_heartbeat(
    kind: str,
    target_id: str,
    prompt: str,
    *,
    interval_s: int = DEFAULT_INTERVAL_S,
    lifetime_s: int = MAX_LIFETIME_S,
    created_by: str = "agent",
) -> Heartbeat:
    """Create or replace a target's heartbeat; its clock starts now."""
    _check_kind(kind)
    target_id = str(target_id)
    validate(prompt, interval_s)
    if lifetime_s <= 0:
        raise HeartbeatError(400, "--for must be positive")
    if lifetime_s > MAX_LIFETIME_S:
        raise HeartbeatError(400, "A heartbeat can last at most 7 days (--for 7d)")
    if created_by not in ("agent", "user"):
        raise HeartbeatError(400, f"Unknown creator {created_by!r}")
    _check_target(kind, target_id)
    hb = hs.store.set(
        kind, target_id, prompt=prompt.strip(), interval_s=interval_s,
        lifetime_s=lifetime_s, created_by=created_by,
    )
    changed(kind, target_id)
    wake()
    return hb


def update(
    kind: str,
    target_id: str,
    *,
    prompt: str | None = None,
    interval_s: int | None = None,
    paused: bool | None = None,
) -> Heartbeat:
    _check_kind(kind)
    target_id = str(target_id)
    if hs.store.get(kind, target_id) is None:
        raise HeartbeatError(404, "No heartbeat on this target")
    fields: dict = {}
    if prompt is not None:
        if not prompt.strip():
            raise HeartbeatError(400, "A heartbeat needs a prompt")
        fields["prompt"] = prompt.strip()
    if interval_s is not None:
        _check_interval(interval_s)
        fields["interval_s"] = interval_s
    if fields:
        hs.store.update(kind, target_id, **fields)
    if paused is not None:
        hs.store.set_paused(kind, target_id, paused)
    changed(kind, target_id)
    wake()
    return hs.store.get(kind, target_id)


def stop(kind: str, target_id: str) -> bool:
    """Delete a target's heartbeat. Whoever stops it, the agent isn't told."""
    _check_kind(kind)
    deleted = hs.store.delete(kind, str(target_id))
    if deleted:
        changed(kind, str(target_id))
    return deleted


def ticking(kind: str, target_id: str) -> bool:
    """Whether a tick is running on the target right now."""
    if kind == CONVERSATION:
        with _in_flight_lock:
            return str(target_id) in _in_flight
    from yuki_conductor import taskboard
    from yuki_conductor.task_store import IN_PROGRESS

    task = taskboard.store.get(int(target_id))
    return bool(task and task.status == IN_PROGRESS and task.heartbeat_return_status)


def serialize(hb: Heartbeat | None, *, is_ticking: bool | None = None) -> dict | None:
    """The API shape. `is_ticking` saves a lookup when the caller knows."""
    if hb is None:
        return None
    if is_ticking is None:
        is_ticking = ticking(hb.target_kind, hb.target_id)
    return {
        "target_kind": hb.target_kind,
        "target_id": hb.target_id,
        "prompt": hb.prompt,
        "interval_s": hb.interval_s,
        "next_due_at": hb.next_due_at,
        "expires_at": hb.expires_at,
        "paused": hb.paused,
        "created_by": hb.created_by,
        "created_at": hb.created_at,
        "last_tick_at": hb.last_tick_at,
        "last_outcome": hb.last_outcome,
        "consecutive_errors": hb.consecutive_errors,
        "ticking": is_ticking,
    }


def changed(kind: str, target_id: str) -> None:
    """Tell browsers a target's heartbeat changed."""
    if kind == TASK:
        from yuki_conductor.taskboard import live

        live.task_changed(int(target_id))
    elif _manager is not None:
        _manager.broadcast(
            target_id, {"type": "heartbeat", "target_kind": kind, "target_id": target_id}
        )


# -- lifecycle ---------------------------------------------------------------


def target_gone(kind: str, target_id: str) -> None:
    """The target was finished, archived or deleted: its heartbeat goes too."""
    if hs.store.delete(kind, str(target_id)):
        changed(kind, str(target_id))
    hs.store.delete_ticks(kind, str(target_id))


def on_task_transition(event) -> None:
    """Taskboard listener. A task moving to Done loses its heartbeat; one
    whose ordinary turn just ended has its next tick counted from now."""
    from yuki_conductor.task_store import DONE, IN_PROGRESS

    if event.to_status == DONE:
        target_gone(TASK, str(event.task.id))
    elif event.from_status == IN_PROGRESS and event.reason != "heartbeat":
        hs.store.postpone(TASK, str(event.task.id))


def _note(kind: str, target_id: str, text: str) -> None:
    """Tell the user something happened to a target's heartbeat, on the target."""
    if kind == TASK:
        from yuki_conductor import taskboard

        taskboard.store.add_comment(int(target_id), "system", text)
        changed(kind, target_id)
        return
    resolved = _resolve_conversation(target_id)
    if resolved is None:
        return
    from yuki_conductor.messaging import OutgoingMessage

    platform, _ = resolved
    try:
        platform.send(target_id, OutgoingMessage(text=f"_♡ {text}_", pushed=True))
    except Exception:
        logger.warning("Couldn't post a heartbeat note to %s", target_id, exc_info=True)


def pause_with_note(kind: str, target_id: str, reason: str) -> None:
    hs.store.set_paused(kind, target_id, True)
    _note(kind, target_id, f"Heartbeat paused: {reason}")
    changed(kind, target_id)


def expire(now: float | None = None) -> list[Heartbeat]:
    """Delete heartbeats past their expiry, with a note on each target."""
    gone = []
    for hb in hs.store.expired(now):
        if hs.store.delete(hb.target_kind, hb.target_id):
            age = format_duration(hb.expires_at - hb.created_at)
            _note(hb.target_kind, hb.target_id, f"Heartbeat expired after {age}")
            changed(hb.target_kind, hb.target_id)
            gone.append(hb)
    return gone


def record_tick(hb: Heartbeat, outcome: str) -> Heartbeat | None:
    """Store a finished tick's outcome; pause after too many errors in a row."""
    updated = hs.store.record_result(hb, outcome, pause_after_errors=MAX_CONSECUTIVE_ERRORS)
    if updated is not None and updated.paused and outcome == ERROR:
        _note(
            hb.target_kind, hb.target_id,
            f"Heartbeat paused after {updated.consecutive_errors} failed ticks in a row. "
            "Resume it from the menu once the problem is fixed.",
        )
    changed(hb.target_kind, hb.target_id)
    return updated


# -- tick prompts --------------------------------------------------------------


def _header(hb: Heartbeat) -> str:
    who = "you" if hb.created_by == "agent" else "the user"
    when = datetime.fromtimestamp(hb.created_at).strftime("%Y-%m-%d %H:%M")
    return (
        f"[Heartbeat — fires every {format_duration(hb.interval_s)}. "
        f"Set by {who} at {when}.]\n{hb.prompt}"
    )


def conversation_prompt(hb: Heartbeat) -> str:
    return (
        f"{_header(hb)}\n\n"
        "This is a scheduled check, not a message from the user. If nothing "
        "changed or nothing needs the user, keep your tool output small and end "
        "with <silence>. Include <notify> only if the user should see this reply. "
        "When the thing you are watching is resolved, run `yuki-conductor heartbeat "
        "stop` before replying."
    )


def announcement(hb: Heartbeat) -> str:
    """For a task's first turn when it was created with a heartbeat."""
    return (
        f"This task has a heartbeat: every {format_duration(hb.interval_s)} after "
        f"a turn ends, your session is woken with: \"{hb.prompt}\". Use the "
        "`yuki-conductor-heartbeat` skill to change or stop it."
    )


def task_prompt(hb: Heartbeat) -> str:
    return (
        f"{_header(hb)}\n\n"
        "This is a scheduled check on your task, not a message from the user. If "
        "nothing changed or nothing needs the user, keep your tool output small and "
        "end with <silence>: the task quietly returns to where it was. Include "
        "<notify> if you acted or have a result the user should see; the reply "
        "becomes a comment and the task moves to Ready for Review. If you need "
        "the user's decision, run `yuki-conductor task ask` as usual. When the "
        "thing you are watching is resolved, run `yuki-conductor heartbeat stop`."
    )


# -- conversation ticks ----------------------------------------------------------

_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def _resolve_conversation(key: str):
    """`(platform, IncomingMessage template)` for a conversation, or None.

    The message carries everything a user turn would resolve: model, cwd,
    and the conversation's MCP and permission settings.
    """
    from yuki_conductor.messaging import IncomingMessage
    from yuki_conductor.web_server import conv_store, platform_for, project_store

    platform = platform_for(key)
    if platform is None:
        return None
    if platform.name == "web":
        conv = conv_store.get_conversation(key)
        if conv is None or conv.archived_at is not None:
            return None
        msg = IncomingMessage(
            platform="web", conversation_key=key, message_id="", text="",
            model=conv.model, wait_for_mcp=bool(conv.wait_for_mcp),
            skip_permissions=bool(conv.skip_permissions),
            cwd=project_store.get_path(conv.project) if conv.project else None,
            kind="heartbeat",
        )
        return platform, msg
    from yuki_conductor.harness import default_harness
    from yuki_conductor.store import ModelStore, SessionStore

    sessions, models = SessionStore(), ModelStore()
    harness = platform.get_harness(key) or default_harness()
    channel = sessions.get_channel(key)
    model = models.get(models.key(key, harness)) or (
        models.get(models.key(channel, harness)) if channel else None
    )
    msg = IncomingMessage(
        platform=platform.name, conversation_key=key, message_id="", text="",
        model=model, kind="heartbeat",
    )
    return platform, msg


def fire_conversation(hb: Heartbeat) -> bool:
    """Start a conversation tick on its own thread, unless the conversation is
    busy (then the row stays due and fires once it's idle). Returns whether
    it started."""
    from yuki_conductor.messaging import conversation

    key = hb.target_id
    with _in_flight_lock:
        if key in _in_flight:
            return False
    resolved = _resolve_conversation(key)
    if resolved is None:
        logger.info("Heartbeat target conversation %s is gone; deleting it", key)
        target_gone(CONVERSATION, key)
        return False
    if not conversation.try_begin(key):
        return False
    with _in_flight_lock:
        _in_flight.add(key)
    changed(CONVERSATION, key)
    threading.Thread(
        target=_run_conversation_tick, args=(hb, resolved), name=f"heartbeat-{key}", daemon=True
    ).start()
    return True


def _run_conversation_tick(hb: Heartbeat, resolved) -> None:
    """Run one tick on a conversation `fire_conversation` has claimed.

    The outcome is recorded before the conversation is released, so a user
    turn waiting on it always schedules the next tick after this one's.
    """
    from yuki_conductor.messaging import conversation

    key = hb.target_id
    tick_id = hs.store.start_tick(CONVERSATION, key)
    try:
        outcome, response, error = _tick_conversation(hb, resolved)
        if outcome is not None:
            record_tick(hb, outcome)
        hs.store.finish_tick(tick_id, outcome or ERROR, response=response, error=error)
    finally:
        conversation.end(key)
        with _in_flight_lock:
            _in_flight.discard(key)
        changed(CONVERSATION, key)


def _tick_conversation(hb: Heartbeat, resolved) -> tuple[str | None, str | None, str | None]:
    """`(outcome, response, error)`; outcome None: paused, not a tick result."""
    from dataclasses import replace

    from yuki_conductor.messaging import conversation
    from yuki_conductor.messaging.conversation import NOTIFY_TAG, HarnessUnavailable

    platform, template = resolved
    key = hb.target_id
    try:
        conversation.resolve_harness(platform, template)
    except HarnessUnavailable as exc:
        pause_with_note(CONVERSATION, key, "this conversation's harness isn't enabled here")
        return None, None, str(exc)
    try:
        result = conversation.run_claimed(platform, replace(template, text=conversation_prompt(hb)))
    except Exception as exc:
        logger.exception("Heartbeat tick on %s crashed", key)
        error = f"{type(exc).__name__}: {exc}"
        platform.send(key, _error_message(error))
        return ERROR, None, error
    if result.is_error:
        error = result.text.strip()[:4000] or "the run failed"
        # Failures are always surfaced.
        platform.send(key, _error_message(error))
        return ERROR, None, error
    outcome = NOTIFIED if NOTIFY_TAG in result.text else QUIET
    return outcome, conversation.strip_tags(result.text), None


def _error_message(error: str):
    from yuki_conductor.messaging import OutgoingMessage

    return OutgoingMessage(
        text=f"_♡ Heartbeat check failed:_\n\n```\n{error}\n```", pushed=True
    )


# -- task ticks (run by the harvester) -----------------------------------------


def due_tasks() -> list[Heartbeat]:
    """Task heartbeats due now, oldest first. The harvester claims them."""
    return hs.store.due(TASK)


# -- the loop --------------------------------------------------------------------

_wake = threading.Event()


def wake() -> None:
    _wake.set()


def tick() -> list[str]:
    """Expire what's past its time and fire due conversation ticks. Returns
    the conversation keys started. Task ticks are the harvester's: waking it
    is enough."""
    expire()
    started = [hb.target_id for hb in hs.store.due(CONVERSATION) if fire_conversation(hb)]
    from yuki_conductor import taskboard

    if taskboard.active_harvester is not None and hs.store.due(TASK):
        taskboard.active_harvester.wake()
    return started


def loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            tick()
        except Exception:
            logger.exception("Heartbeat scheduler tick failed")
        _wake.wait(POLL_SECONDS)
        _wake.clear()


def start(ws_manager=None) -> threading.Event:
    """Start the scheduler. Overdue heartbeats fire once, spread over a minute."""
    global _manager
    _manager = ws_manager
    spread = hs.store.spread_overdue(lambda: random.uniform(0, STARTUP_JITTER_S))
    if spread:
        logger.info("Heartbeats: %d overdue after restart, firing within %ss", spread, STARTUP_JITTER_S)
    stop_event = threading.Event()
    threading.Thread(target=loop, args=(stop_event,), name="heartbeat-scheduler", daemon=True).start()
    return stop_event

"""Platform-agnostic core: turn an IncomingMessage into an agent run + reply."""

import logging
import re
import threading

from yuki_conductor import heartbeat_store
from yuki_conductor.config import ATTACHMENTS_DIR
from yuki_conductor.harness import HARNESSES, default_harness, get_harness
from yuki_conductor.harness.base import RunResult
from yuki_conductor.harness.process import run_agent
from yuki_conductor.messaging.platform import (
    Attachment,
    IncomingMessage,
    MessagingPlatform,
    OutgoingMessage,
)

logger = logging.getLogger(__name__)

_ATTACHMENT_RE = re.compile(r"<attachment>(.*?)</attachment>")


def _build_prompt(text: str, attachments: list[Attachment]) -> str:
    """Prepend <attachment> tags so Claude can read uploaded files from disk."""
    if not attachments:
        return text
    prefix = "".join(f"<attachment>{a.local_path}</attachment>\n" for a in attachments)
    return prefix + text


# Platforms whose conversation keys `/api/push` can route back to.
PUSHABLE_PLATFORMS = ("web", "slack")

# Appended to a heartbeat tick's reply, so a wake-up the user didn't ask for
# reads differently from a reply to something they said (cf. PUSH_MARKER).
HEARTBEAT_MARKER = "_♡ Heartbeat_"

NOTIFY_TAG = "<notify>"
SILENCE_TAG = "<silence>"


def strip_tags(text: str) -> str:
    """The reply without its <notify>/<silence> tags."""
    return text.replace(NOTIFY_TAG, "").replace(SILENCE_TAG, "").strip()


# One turn per conversation at a time. Without it two turns could resume the
# same session in parallel: a user message arriving during a heartbeat tick
# waits for the tick instead.
_turn_locks: dict[str, threading.Lock] = {}
_turn_locks_guard = threading.Lock()


def _turn_lock(key: str) -> threading.Lock:
    with _turn_locks_guard:
        return _turn_locks.setdefault(key, threading.Lock())


def try_begin(key: str) -> bool:
    """Claim the conversation for a turn without waiting. False: one is running."""
    return _turn_lock(key).acquire(blocking=False)


def begin(key: str) -> None:
    """Claim the conversation for a turn, waiting for a running one to end."""
    _turn_lock(key).acquire()


def end(key: str) -> None:
    _turn_lock(key).release()


def busy(key: str) -> bool:
    return _turn_lock(key).locked()


def split_response(text: str) -> tuple[str, list[Attachment]]:
    """Pull <attachment>path</attachment> tags out of Claude's reply.

    Absolute paths are used as-is; bare filenames are looked up under
    ATTACHMENTS_DIR. Missing files are dropped with a warning rather than
    failing the whole reply.
    """
    matches = _ATTACHMENT_RE.findall(text)
    if not matches:
        return text, []

    attachments: list[Attachment] = []
    for ref in matches:
        # Joining an absolute path onto a directory yields the absolute path.
        path = ATTACHMENTS_DIR / ref.strip()
        if not path.is_file():
            logger.warning(f"Claude referenced missing attachment: {path}")
            continue
        attachments.append(Attachment(filename=path.name, local_path=path))

    cleaned = _ATTACHMENT_RE.sub("", text).strip()
    return cleaned, attachments


def _event_sink(platform: MessagingPlatform, conversation_key: str):
    """Adapt a platform's optional `on_stream_event` into a run_agent callback.

    Platforms predating streaming (and test stubs) may not implement the hook;
    those runs simply produce no intermediate updates.
    """
    handler = getattr(platform, "on_stream_event", None)
    if handler is None:
        return None

    def sink(event) -> None:
        try:
            handler(conversation_key, event)
        except Exception:
            logger.debug("Stream event delivery failed", exc_info=True)

    return sink


class HarnessUnavailable(Exception):
    """The conversation is bound to a harness this machine doesn't enable."""


def resolve_harness(platform: MessagingPlatform, msg: IncomingMessage) -> str:
    """Pick the harness for this turn.

    Whatever the platform already recorded for the conversation wins, even on
    its first message: a web conversation is created with its harness before
    anything is sent, and session ids only work with the harness that created
    them. If that harness isn't enabled here, the turn fails rather than
    quietly switching harness. With no record (a brand-new Slack thread), the
    message's choice applies if enabled, else the default harness.
    """
    bound = platform.get_harness(msg.conversation_key)
    if bound:
        if bound not in HARNESSES:
            raise HarnessUnavailable(
                f"This conversation runs on {bound}, which isn't enabled on this "
                "machine (see the HARNESSES setting). Start a new conversation instead."
            )
        return bound
    name = msg.harness or default_harness()
    if name not in HARNESSES:
        logger.warning(
            "Harness %r for %s isn't enabled; using %s",
            name, msg.conversation_key, default_harness(),
        )
        name = default_harness()
    return name


def handle_incoming_message(
    platform: MessagingPlatform,
    msg: IncomingMessage,
) -> RunResult:
    """Run the conversation's agent for a user's message and send the reply.

    Waits for any turn already running on the conversation (a heartbeat
    tick), showing the conversation as busy meanwhile. Returns the result.
    """
    platform.set_processing(msg.conversation_key, msg.message_id, on=True)
    try:
        begin(msg.conversation_key)
        try:
            return run_claimed(platform, msg)
        finally:
            end(msg.conversation_key)
            # The agent just had a turn: the next heartbeat tick is one
            # interval from now.
            heartbeat_store.store.postpone(heartbeat_store.CONVERSATION, msg.conversation_key)
    finally:
        platform.set_processing(msg.conversation_key, msg.message_id, on=False)


def run_claimed(platform: MessagingPlatform, msg: IncomingMessage) -> RunResult:
    """One turn, on a conversation the caller has claimed (`begin`/`try_begin`).

    This is the only place that knows about session resolution, attachment
    plumbing, and the run_agent contract. Platforms own their own session-id
    persistence (Slack via SessionStore, web via ConversationStore).

    A heartbeat tick (`msg.kind == "heartbeat"`) shows no progress, and its
    reply is only delivered if it contains <notify>; failures are left to the
    caller to report.
    """
    heartbeat = msg.kind == "heartbeat"
    session_id = (
        None if msg.is_thread_start else platform.get_session_id(msg.conversation_key)
    )

    try:
        harness = resolve_harness(platform, msg)
    except HarnessUnavailable as exc:
        logger.warning("%s: %s", msg.conversation_key, exc)
        if not heartbeat:
            platform.send(msg.conversation_key, OutgoingMessage(text=str(exc)))
        return RunResult(text=str(exc), session_id=session_id, is_error=True)
    prompt = _build_prompt(msg.text, msg.attachments)
    # Native to the conversation's harness; None: the harness's default.
    model = msg.model
    logger.info(
        "Running %s for %s (%s, session=%s, model=%s)",
        harness, msg.conversation_key, msg.kind, session_id, model,
    )
    try:
        result = run_agent(
            get_harness(harness),
            prompt,
            session_id=session_id,
            model=model,
            wait_for_mcp=msg.wait_for_mcp,
            skip_permissions=msg.skip_permissions,
            conversation_key=msg.conversation_key,
            cwd=msg.cwd,
            # Only keys `/api/push` can route are advertised.
            push_conversation_id=(
                msg.conversation_key if msg.platform in PUSHABLE_PLATFORMS else None
            ),
            # A heartbeat tick is invisible unless it notifies.
            on_event=None if heartbeat else _event_sink(platform, msg.conversation_key),
            kind="heartbeat" if heartbeat else "chat",
        )
    except Exception:
        logger.error(
            "%s invocation failed for %s", harness, msg.conversation_key, exc_info=True,
        )
        raise
    logger.info(
        "%s finished for %s (tokens: %s in / %s out, cost: $%s)",
        harness, msg.conversation_key,
        result.input_tokens, result.output_tokens, result.cost_usd,
    )

    if result.session_id:
        platform.set_session_id(
            msg.conversation_key,
            result.session_id,
            title_hint=msg.title_hint if msg.is_thread_start else None,
            harness=harness,
        )

    text = result.text
    if heartbeat:
        if result.is_error or NOTIFY_TAG not in text:
            return result
        text = f"{strip_tags(text)}\n\n{HEARTBEAT_MARKER}"
    reply_text, reply_attachments = split_response(text)
    outgoing = OutgoingMessage(
        text=reply_text,
        attachments=reply_attachments,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        context_tokens=result.context_tokens,
        context_window=result.context_window,
        model=result.model,
        # No turn bookends a tick's reply: deliver it like a push.
        pushed=heartbeat,
    )
    logger.info(
        "Sending reply for %s (%d chars, %d attachments)",
        msg.conversation_key, len(reply_text), len(reply_attachments),
    )
    platform.send(msg.conversation_key, outgoing)
    logger.info("Reply sent for %s", msg.conversation_key)
    return result

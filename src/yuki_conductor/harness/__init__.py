"""Agent harnesses: the CLIs yuki-conductor can spawn for a turn.

See docs/plans/2026-10-12-multi-harness.md. Which harnesses exist on this
machine is the `HARNESSES` setting (config.ENABLED_HARNESSES); only those are
registered, so a disabled harness can't be picked, scheduled or run. Callers
pick one by name with `get_harness` and run a turn with `process.run_agent`.
"""

import logging

from yuki_conductor.config import ENABLED_HARNESSES
from yuki_conductor.harness.base import Harness, RunResult, StreamEvent
from yuki_conductor.harness.claude_code import ClaudeCodeHarness
from yuki_conductor.harness.codex import CodexHarness
from yuki_conductor.harness.pi import PiHarness

logger = logging.getLogger(__name__)

KNOWN_HARNESSES: dict[str, Harness] = {
    h.name: h for h in (ClaudeCodeHarness(), PiHarness(), CodexHarness())
}


def _enabled(names: list[str]) -> dict[str, Harness]:
    enabled: dict[str, Harness] = {}
    for name in names:
        if name in KNOWN_HARNESSES:
            enabled[name] = KNOWN_HARNESSES[name]
        else:
            logger.warning("HARNESSES names unknown harness %r; ignoring it", name)
    if not enabled:
        logger.warning("HARNESSES enables nothing usable; falling back to claude-code")
        enabled["claude-code"] = KNOWN_HARNESSES["claude-code"]
    return enabled


# Enabled harnesses in preference order; the first is the default.
HARNESSES: dict[str, Harness] = _enabled(ENABLED_HARNESSES)


def default_harness() -> str:
    """The harness a new conversation gets when it names none."""
    return next(iter(HARNESSES))


def get_harness(name: str) -> Harness:
    """Return the enabled harness called `name`. Raises KeyError otherwise."""
    return HARNESSES[name]


__all__ = [
    "HARNESSES", "KNOWN_HARNESSES", "Harness", "RunResult", "StreamEvent",
    "default_harness", "get_harness",
]

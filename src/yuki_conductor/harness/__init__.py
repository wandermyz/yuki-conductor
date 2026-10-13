"""Agent harnesses: the CLIs yuki-conductor can spawn for a turn.

See docs/plans/2026-10-12-multi-harness.md. Callers pick a harness by name
with `get_harness` and run a turn with `process.run_agent`.
"""

from yuki_conductor.harness.base import Harness, RunResult, StreamEvent
from yuki_conductor.harness.claude_code import ClaudeCodeHarness
from yuki_conductor.harness.pi import PiHarness

HARNESSES: dict[str, Harness] = {
    h.name: h for h in (ClaudeCodeHarness(), PiHarness())
}


def get_harness(name: str) -> Harness:
    """Return the harness called `name`. Raises KeyError for an unknown name."""
    return HARNESSES[name]


__all__ = ["HARNESSES", "Harness", "RunResult", "StreamEvent", "get_harness"]

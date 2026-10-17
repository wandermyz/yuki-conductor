"""The HARNESSES setting decides which harnesses exist on this machine."""

from yuki_conductor import harness


def test_enabled_keeps_order_and_known_names_only():
    enabled = harness._enabled(["pi", "nope", "claude-code"])
    assert list(enabled) == ["pi", "claude-code"]
    assert enabled["pi"] is harness.KNOWN_HARNESSES["pi"]


def test_enabling_nothing_usable_falls_back_to_claude_code():
    assert list(harness._enabled([])) == ["claude-code"]
    assert list(harness._enabled(["nope"])) == ["claude-code"]


def test_default_is_the_first_enabled(monkeypatch):
    monkeypatch.setattr(harness, "HARNESSES", harness._enabled(["pi", "claude-code"]))
    assert harness.default_harness() == "pi"


def test_env_parsing():
    from yuki_conductor.config import parse_harnesses

    assert parse_harnesses(" claude-code , pi ,") == ["claude-code", "pi"]
    assert parse_harnesses(None) == ["claude-code"]
    assert parse_harnesses("") == ["claude-code"]

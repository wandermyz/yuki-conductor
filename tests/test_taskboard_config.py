"""TASKBOARD_CONCURRENCY parsing and per-harness limits."""

import pytest

from yuki_conductor import config
from yuki_conductor.config import parse_taskboard_concurrency, taskboard_limit


def test_parse():
    assert parse_taskboard_concurrency("claude-code:4, pi:2") == {"claude-code": 4, "pi": 2}
    assert parse_taskboard_concurrency(None) == {}
    assert parse_taskboard_concurrency(" , ") == {}


@pytest.mark.parametrize("raw", ["claude-code", "pi:0", "pi:-1", "pi:x", ":3"])
def test_parse_rejects(raw):
    with pytest.raises(RuntimeError, match="TASKBOARD_CONCURRENCY"):
        parse_taskboard_concurrency(raw)


def test_limit_defaults(monkeypatch):
    monkeypatch.setattr(config, "TASKBOARD_CONCURRENCY", {})
    assert taskboard_limit("claude-code") == 10
    assert taskboard_limit("pi") == 1
    assert taskboard_limit("other") == 1


def test_limit_override(monkeypatch):
    monkeypatch.setattr(config, "TASKBOARD_CONCURRENCY", {"pi": 3})
    assert taskboard_limit("pi") == 3
    assert taskboard_limit("claude-code") == 10

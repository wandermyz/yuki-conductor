"""Slack's `/yuki-harness` and how a new thread picks its harness.

The Bolt app is built with a stand-in `App` that just records the handlers
each decorator registers, so they can be called directly, with no Slack
connection.
"""

from unittest.mock import MagicMock

import pytest

from yuki_conductor import harness as harness_pkg
from yuki_conductor import slack_app
from yuki_conductor.store import HarnessStore, SessionStore


class FakeApp:
    def __init__(self, token=None):
        self.commands = {}
        self.events = {}

    def command(self, name):
        def register(fn):
            self.commands[name] = fn
            return fn
        return register

    def event(self, name):
        def register(fn):
            self.events[name] = fn
            return fn
        return register


@pytest.fixture
def app(monkeypatch, tmp_path):
    """create_app() with throwaway stores and both harnesses enabled."""
    db = tmp_path / "t.db"
    monkeypatch.setattr(slack_app, "App", FakeApp)
    monkeypatch.setattr(slack_app, "slack_bot_token", lambda: "xoxb-test")
    monkeypatch.setattr(slack_app, "store", SessionStore(db_path=db))
    monkeypatch.setattr(slack_app, "harness_store", HarnessStore(db_path=db))
    monkeypatch.setattr(slack_app, "model_store", MagicMock(get=lambda c: None, list_all=lambda: []))

    saved = dict(harness_pkg.HARNESSES)
    harness_pkg.HARNESSES.clear()
    harness_pkg.HARNESSES.update({
        "claude-code": harness_pkg.KNOWN_HARNESSES["claude-code"],
        "pi": harness_pkg.KNOWN_HARNESSES["pi"],
    })
    yield slack_app.create_app()
    harness_pkg.HARNESSES.clear()
    harness_pkg.HARNESSES.update(saved)


def _harness_cmd(app, text, channel="C1"):
    respond = MagicMock()
    app.commands["/yuki-harness"](ack=MagicMock(), command={"channel_id": channel, "text": text},
                                  respond=respond)
    return respond.call_args.args[0]


def test_show_set_and_reset(app):
    assert "*claude-code* (the default)" in _harness_cmd(app, "")
    assert "now use *pi*" in _harness_cmd(app, "pi")
    assert slack_app.channel_harness("C1") == "pi"
    assert slack_app.channel_harness("C2") == "claude-code"  # per channel
    assert "*pi*" in _harness_cmd(app, "")
    assert "default harness" in _harness_cmd(app, "default")
    assert slack_app.channel_harness("C1") == "claude-code"


def test_refuses_a_harness_not_enabled_here(app):
    reply = _harness_cmd(app, "dsh")
    assert "isn't enabled" in reply
    assert slack_app.harness_store.get("C1") is None


def test_a_pin_to_a_since_disabled_harness_falls_back(app):
    slack_app.harness_store.set("C1", "pi")
    del harness_pkg.HARNESSES["pi"]
    assert slack_app.channel_harness("C1") == "claude-code"
    assert "isn't enabled on this machine" in _harness_cmd(app, "")


def _post(app, monkeypatch, **event):
    """Deliver a Slack message event; return the IncomingMessage handed to the core."""
    started = []
    monkeypatch.setattr(
        slack_app.threading, "Thread",
        lambda target, args, **kw: MagicMock(start=lambda: started.append(args[1])),
    )
    app.events["message"](event={"channel": "C1", "ts": "1.0", "text": "hi", **event},
                          client=MagicMock())
    return started[0] if started else None


def test_new_thread_starts_on_the_channel_harness(app, monkeypatch):
    """Regression: the pre-created row used to lock every Slack thread to claude-code."""
    slack_app.harness_store.set("C1", "pi")
    msg = _post(app, monkeypatch)
    assert msg.harness == "pi"
    assert slack_app.store.get_harness("1.0") == "pi"


def test_reply_keeps_its_threads_harness_after_the_channel_changes(app, monkeypatch):
    slack_app.store.set("1.0", "sess-1", channel_id="C1", harness="pi")
    slack_app.harness_store.set("C1", "claude-code")
    msg = _post(app, monkeypatch, ts="2.0", thread_ts="1.0")
    assert msg.harness is None  # the core reads the thread's own record
    assert slack_app.store.get_harness("1.0") == "pi"

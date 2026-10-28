"""Slack's `/yuki-harness` and how a new thread picks its harness.

The Bolt app is built with a stand-in `App` that just records the handlers
each decorator registers, so they can be called directly, with no Slack
connection.
"""

from unittest.mock import MagicMock

import pytest

from yuki_conductor import harness as harness_pkg
from yuki_conductor import slack_app
from yuki_conductor.store import HarnessStore, ModelStore, SessionStore


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
    # Pin the prefix: the real .env may set SLACK_COMMAND_PREFIX for this machine.
    monkeypatch.setattr(slack_app, "slack_command_prefix", lambda: "yuki")
    monkeypatch.setattr(slack_app, "store", SessionStore(db_path=db))
    monkeypatch.setattr(slack_app, "harness_store", HarnessStore(db_path=db))
    monkeypatch.setattr(slack_app, "model_store", ModelStore(db_path=db))

    saved = dict(harness_pkg.HARNESSES)
    harness_pkg.HARNESSES.clear()
    harness_pkg.HARNESSES.update({
        "claude-code": harness_pkg.KNOWN_HARNESSES["claude-code"],
        "pi": harness_pkg.KNOWN_HARNESSES["pi"],
        "codex": harness_pkg.KNOWN_HARNESSES["codex"],
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


def _model_cmd(app, text, channel="C1"):
    respond = MagicMock()
    app.commands["/yuki-model"](ack=MagicMock(), command={"channel_id": channel, "text": text},
                                respond=respond)
    return respond.call_args.args[0]


def test_model_command_uses_the_channels_harness_aliases(app):
    # Claude Code (the default harness here): its own aliases.
    assert "default (opus)" in _model_cmd(app, "")
    assert "switched to *sonnet*" in _model_cmd(app, "sonnet")
    assert "Unknown model `astra`" in _model_cmd(app, "astra")
    # Switch the channel to Codex: astra / sol, and the Claude pin is untouched.
    _harness_cmd(app, "codex")
    assert "default (sol)" in _model_cmd(app, "")
    assert "switched to *astra* (`gpt-6-astra`)" in _model_cmd(app, "astra")
    assert "*astra*" in _model_cmd(app, "")
    assert slack_app.model_store.get(ModelStore.key("C1", "codex")) == "gpt-6-astra"
    assert slack_app.model_store.get(ModelStore.key("C1", "claude-code")) == "sonnet"
    assert "reset to the default (sol)" in _model_cmd(app, "default")
    assert slack_app.model_store.get(ModelStore.key("C1", "claude-code")) == "sonnet"


def test_model_command_on_a_harness_without_choices(app):
    _harness_cmd(app, "pi")
    assert "no models to pick from" in _model_cmd(app, "astra")


def test_thread_gets_the_pin_for_its_own_harness(app, monkeypatch):
    slack_app.model_store.set(ModelStore.key("C1", "claude-code"), "sonnet")
    slack_app.model_store.set(ModelStore.key("C1", "codex"), "gpt-6-astra")
    assert _post(app, monkeypatch).model == "sonnet"
    slack_app.harness_store.set("C1", "codex")
    assert _post(app, monkeypatch, ts="2.0").model == "gpt-6-astra"
    slack_app.harness_store.set("C1", "pi")
    assert _post(app, monkeypatch, ts="3.0").model is None
    # A reply follows its thread's harness, not the channel's current one.
    slack_app.store.set("4.0", "sess-4", channel_id="C1", harness="claude-code")
    assert _post(app, monkeypatch, ts="5.0", thread_ts="4.0").model == "sonnet"


# --------------------------------------------------------------------------
# SLACK_COMMAND_PREFIX
# --------------------------------------------------------------------------


def _handler_for(app, command: str):
    """Resolve a slash command the way Bolt does: exact names, then the regex catch-all."""
    if command in app.commands:
        return app.commands[command]
    for key, fn in app.commands.items():
        if hasattr(key, "fullmatch") and key.fullmatch(command):
            return fn
    return None


def test_commands_use_the_configured_prefix(app, monkeypatch):
    monkeypatch.setattr(slack_app, "slack_command_prefix", lambda: "skirk")
    skirk = slack_app.create_app()
    names = {k for k in skirk.commands if isinstance(k, str)}
    assert names == {"/skirk-harness", "/skirk-model", "/skirk-title", "/skirk-usage"}
    # Only this machine's commands: another machine's /yuki-* isn't claimed.
    assert _handler_for(skirk, "/yuki-model") is None
    assert _handler_for(skirk, "/yuki-review") is None
    # The catch-all forwards /skirk-<skill> as Claude's /<skill>.
    assert _handler_for(skirk, "/skirk-review") is _handler_for(skirk, "/skirk-anything")


def test_replies_name_the_prefixed_command(app, monkeypatch):
    monkeypatch.setattr(slack_app, "slack_command_prefix", lambda: "skirk")
    skirk = slack_app.create_app()
    respond = MagicMock()
    skirk.commands["/skirk-harness"](ack=MagicMock(), command={"channel_id": "C1", "text": ""},
                                     respond=respond)
    assert "`/skirk-harness [" in respond.call_args.args[0]


def test_catch_all_strips_the_prefix(app, monkeypatch):
    monkeypatch.setattr(slack_app, "slack_command_prefix", lambda: "skirk")
    skirk = slack_app.create_app()
    ran = {}

    def fake_run(harness, prompt, **kw):
        ran["prompt"] = prompt
        return MagicMock(text="")

    monkeypatch.setattr(slack_app, "run_agent", fake_run)
    _handler_for(skirk, "/skirk-review")(
        ack=MagicMock(), respond=MagicMock(), client=MagicMock(),
        command={"command": "/skirk-review", "channel_id": "C1", "text": "PR 12"},
    )
    assert ran["prompt"] == "/review PR 12"


def test_prefix_validation():
    from yuki_conductor.config import parse_slack_command_prefix

    assert parse_slack_command_prefix(None) == "yuki"
    assert parse_slack_command_prefix(" skirk ") == "skirk"
    assert parse_slack_command_prefix("box-2") == "box-2"
    for bad in ("Skirk", "sk irk", "-x", "x-", "x/y", "/yuki"):
        with pytest.raises(RuntimeError, match="SLACK_COMMAND_PREFIX"):
            parse_slack_command_prefix(bad)

"""Heartbeats: store, scheduler, conversation ticks, task ticks, API and CLI."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import cli_heartbeat, heartbeat, taskboard, web_server
from yuki_conductor import heartbeat_store as hs
from yuki_conductor.cli import main
from yuki_conductor.harness.base import RunResult
from yuki_conductor.heartbeat_store import CONVERSATION, TASK
from yuki_conductor.messaging import IncomingMessage, OutgoingMessage, conversation
from yuki_conductor.task_store import DONE, IN_PROGRESS, NEEDS_INPUT, READY_FOR_REVIEW, TODO
from yuki_conductor.taskboard import actions, routes, runner, transitions
from yuki_conductor.taskboard import harvester as harvester_mod
from yuki_conductor.taskboard.harvester import Harvester
from yuki_conductor.taskboard.transitions import claim, transition

# -- durations -----------------------------------------------------------------


@pytest.mark.parametrize("text,seconds", [
    ("270", 270), ("270s", 270), ("60m", 3600), ("1h", 3600), ("3d", 259200), (" 2H ", 7200),
])
def test_parse_duration(text, seconds):
    assert heartbeat.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "1.5h", "soon", "1w"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        heartbeat.parse_duration(text)


@pytest.mark.parametrize("seconds,text", [
    (270, "4m30s"), (3600, "1h"), (90000, "1d1h"), (45, "45s"), (604800, "7d"),
])
def test_format_duration(seconds, text):
    assert heartbeat.format_duration(seconds) == text


# -- store ---------------------------------------------------------------------


def _set(kind=CONVERSATION, target="c1", interval=270, lifetime=hs.MAX_LIFETIME_S, **kw):
    return hs.store.set(
        kind, target, prompt=kw.get("prompt", "check it"), interval_s=interval,
        lifetime_s=lifetime, created_by=kw.get("created_by", "agent"),
    )


def test_store_due_skips_paused_and_expired():
    a = _set(target="a")
    _set(target="b")
    _set(target="c", lifetime=1)
    hs.store.set_paused(CONVERSATION, "b", True)
    later = a.next_due_at + 1
    assert [hb.target_id for hb in hs.store.due(CONVERSATION, now=later)] == ["a"]
    assert [hb.target_id for hb in hs.store.expired(now=later)] == ["c"]


def test_resume_reschedules_and_clears_errors():
    hb = _set()
    for _ in range(3):
        hs.store.record_result(hb, hs.ERROR, pause_after_errors=3)
    paused = hs.store.get(CONVERSATION, "c1")
    assert paused.paused and paused.consecutive_errors == 3
    resumed = hs.store.set_paused(CONVERSATION, "c1", False)
    assert not resumed.paused and resumed.consecutive_errors == 0
    assert resumed.next_due_at > time.time() + 200


def test_record_result_ignores_a_replaced_heartbeat():
    old = _set(prompt="old")
    time.sleep(0.01)
    _set(prompt="new")
    assert hs.store.record_result(old, hs.QUIET, pause_after_errors=3) is None
    assert hs.store.get(CONVERSATION, "c1").last_outcome is None


def test_ticks_are_pruned_per_target(monkeypatch):
    monkeypatch.setattr(hs, "MAX_TICKS_PER_TARGET", 3)
    for _ in range(5):
        hs.store.finish_tick(hs.store.start_tick(TASK, "1"), hs.QUIET)
    hs.store.start_tick(TASK, "2")
    assert len(hs.store.ticks(TASK, "1")) == 3
    assert len(hs.store.ticks(TASK, "2")) == 1


def test_spread_overdue_on_startup():
    hb = _set()
    now = hb.next_due_at + 1000
    assert hs.store.spread_overdue(lambda: 30, now=now) == 1
    assert hs.store.get(CONVERSATION, "c1").next_due_at == now + 30


def test_postpone_counts_from_the_last_turn():
    hb = _set()
    later = hb.next_due_at + 500
    hs.store.postpone(CONVERSATION, "c1", now=later)
    assert hs.store.get(CONVERSATION, "c1").next_due_at == later + 270
    hs.store.postpone(CONVERSATION, "c1", now=hb.created_at)  # never earlier
    assert hs.store.get(CONVERSATION, "c1").next_due_at == later + 270


# -- the turn guard ------------------------------------------------------------


def test_try_begin_refuses_a_busy_conversation():
    assert conversation.try_begin("busy-1")
    try:
        assert not conversation.try_begin("busy-1")
        assert conversation.busy("busy-1")
        assert conversation.try_begin("other-1")
        conversation.end("other-1")
    finally:
        conversation.end("busy-1")
    assert not conversation.busy("busy-1")


class FakePlatform:
    name = "web"

    def __init__(self):
        self.sent: list[OutgoingMessage] = []
        self.processing: list[bool] = []
        self.session = None

    def send(self, key, msg):
        self.sent.append(msg)

    def set_processing(self, key, message_id, on):
        self.processing.append(on)

    def get_session_id(self, key):
        return self.session

    def get_harness(self, key):
        return "claude-code"

    def set_session_id(self, key, session_id, title_hint=None, harness="claude-code"):
        self.session = session_id


def test_user_message_waits_for_a_running_tick(monkeypatch):
    order = []
    monkeypatch.setattr(
        conversation, "run_agent",
        lambda *a, **k: order.append("user") or RunResult(text="hi", session_id="s"),
    )
    platform = FakePlatform()
    assert conversation.try_begin("conv-wait")  # a tick holds it
    msg = IncomingMessage(platform="web", conversation_key="conv-wait", message_id="m", text="yo")
    t = threading.Thread(target=conversation.handle_incoming_message, args=(platform, msg))
    t.start()
    time.sleep(0.2)
    assert order == [] and platform.processing == [True]  # shown busy, waiting
    order.append("tick done")
    conversation.end("conv-wait")
    t.join(5)
    assert order == ["tick done", "user"]
    assert platform.processing == [True, False]


@pytest.mark.parametrize("text,delivered", [
    ("all quiet <silence>", False),
    ("nothing to say", False),
    ("granted! <notify>", True),
])
def test_heartbeat_turn_delivers_only_notify(monkeypatch, text, delivered):
    seen = {}

    def fake(*a, **k):
        seen.update(k)
        return RunResult(text=text, session_id="s2")

    monkeypatch.setattr(conversation, "run_agent", fake)
    platform = FakePlatform()
    msg = IncomingMessage(
        platform="web", conversation_key="k", message_id="", text="tick", kind="heartbeat"
    )
    result = conversation.run_claimed(platform, msg)
    assert result.text == text
    assert platform.session == "s2"  # the session still advances
    assert platform.processing == []  # no progress UI
    assert seen["on_event"] is None and seen["kind"] == "heartbeat"
    if delivered:
        (out,) = platform.sent
        assert out.pushed and "<notify>" not in out.text
        assert out.text.endswith(conversation.HEARTBEAT_MARKER)
    else:
        assert platform.sent == []


# -- conversation ticks through the web API --------------------------------------


@pytest.fixture
def web(monkeypatch, tmp_path):
    db = tmp_path / "yuki.db"
    from yuki_conductor.conversation_store import ConversationStore
    from yuki_conductor.store import SessionStore

    monkeypatch.setattr(web_server, "store", SessionStore(db_path=db))
    monkeypatch.setattr(web_server, "conv_store", ConversationStore(db_path=db))
    monkeypatch.setattr(taskboard, "active_harvester", None)
    return TestClient(web_server.create_api())


def _conv(web):
    return web.post("/api/conversations", json={"title": "watch"}).json()["id"]


def _wait_tick(kind, target, n=1):
    deadline = time.time() + 5
    while time.time() < deadline:
        ticks = hs.store.ticks(kind, target)
        if len(ticks) >= n and all(t.finished_at for t in ticks):
            return ticks
        time.sleep(0.02)
    raise AssertionError("tick never finished")


def _make_due(kind, target):
    hb = hs.store.get(kind, target)
    hs.store._execute(
        "UPDATE heartbeats SET next_due_at = ? WHERE target_kind = ? AND target_id = ?",
        (time.time() - 1, kind, str(target)),
    )
    return hb


def test_api_set_get_patch_delete(web):
    cid = _conv(web)
    path = f"/api/heartbeats/conversation/{cid}"
    assert web.get(path).json() == {"heartbeat": None, "ticks": []}
    r = web.put(path, json={"prompt": "check the deploy", "interval_s": 3600})
    assert r.status_code == 200, r.text
    hb = r.json()
    assert hb["interval_s"] == 3600 and hb["created_by"] == "user" and not hb["paused"]
    assert hb["expires_at"] == pytest.approx(hb["created_at"] + 7 * 86400)
    r = web.patch(path, json={"paused": True, "interval_s": 120})
    assert r.json()["paused"] and r.json()["interval_s"] == 120
    assert web.delete(path).status_code == 200
    assert web.get(path).json()["heartbeat"] is None
    assert web.delete(path).status_code == 404


@pytest.mark.parametrize("body,detail", [
    ({"prompt": "x", "interval_s": 59}, "at least 60s"),
    ({"prompt": "x", "lifetime_s": 8 * 86400}, "at most 7 days"),
    ({"prompt": "  "}, "needs a prompt"),
])
def test_api_rejects_bad_heartbeats(web, body, detail):
    r = web.put(f"/api/heartbeats/conversation/{_conv(web)}", json=body)
    assert r.status_code == 400 and detail in r.json()["detail"]


def test_api_rejects_unknown_targets(web):
    assert web.put("/api/heartbeats/conversation/nope", json={"prompt": "x"}).status_code == 404
    assert web.put("/api/heartbeats/task/999", json={"prompt": "x"}).status_code == 404
    assert web.get("/api/heartbeats/planet/1").status_code == 404


def test_conversation_tick_quiet_then_notify(web, monkeypatch):
    replies = iter(["nothing yet <silence>", "access granted <notify>"])
    prompts = []

    def fake(harness, prompt, **k):
        prompts.append(prompt)
        return RunResult(text=next(replies), session_id="sess")

    monkeypatch.setattr(conversation, "run_agent", fake)
    cid = _conv(web)
    heartbeat.set_heartbeat(CONVERSATION, cid, "check the grant", interval_s=60)

    _make_due(CONVERSATION, cid)
    assert heartbeat.tick() == [cid]
    (tick,) = _wait_tick(CONVERSATION, cid)
    assert tick.outcome == hs.QUIET
    assert "check the grant" in prompts[0] and "fires every 1m" in prompts[0]
    assert web.get(f"/api/conversations/{cid}/messages").json() == []
    hb = hs.store.get(CONVERSATION, cid)
    assert hb.last_outcome == hs.QUIET and hb.next_due_at > time.time() + 50

    _make_due(CONVERSATION, cid)
    heartbeat.tick()
    _wait_tick(CONVERSATION, cid, n=2)
    (msg,) = web.get(f"/api/conversations/{cid}/messages").json()
    assert msg["role"] == "assistant" and msg["text"].startswith("access granted")
    assert web_server.conv_store.get_all_statuses()[cid] == "unread"
    assert hs.store.get(CONVERSATION, cid).last_outcome == hs.NOTIFIED


def test_busy_conversation_defers_its_tick(web, monkeypatch):
    monkeypatch.setattr(
        conversation, "run_agent", lambda *a, **k: RunResult(text="<silence>", session_id="s")
    )
    cid = _conv(web)
    heartbeat.set_heartbeat(CONVERSATION, cid, "x", interval_s=60)
    _make_due(CONVERSATION, cid)
    assert conversation.try_begin(cid)
    try:
        assert heartbeat.tick() == []
        assert hs.store.ticks(CONVERSATION, cid) == []
    finally:
        conversation.end(cid)
    assert heartbeat.tick() == [cid]  # still due: fires once idle
    _wait_tick(CONVERSATION, cid)


def test_three_failures_pause_and_surface(web, monkeypatch):
    monkeypatch.setattr(
        conversation, "run_agent",
        lambda *a, **k: RunResult(text="boom", session_id=None, is_error=True),
    )
    cid = _conv(web)
    heartbeat.set_heartbeat(CONVERSATION, cid, "x", interval_s=60)
    for n in range(1, 4):
        _make_due(CONVERSATION, cid)
        heartbeat.tick()
        _wait_tick(CONVERSATION, cid, n=n)
    hb = hs.store.get(CONVERSATION, cid)
    assert hb.paused and hb.consecutive_errors == 3
    texts = [m["text"] for m in web.get(f"/api/conversations/{cid}/messages").json()]
    assert sum("check failed" in t for t in texts) == 3
    assert "paused after 3 failed ticks" in texts[-1]


def test_expiry_deletes_with_a_note(web):
    cid = _conv(web)
    hb = heartbeat.set_heartbeat(CONVERSATION, cid, "x", lifetime_s=3600)
    assert heartbeat.expire(now=hb.expires_at + 1)
    assert hs.store.get(CONVERSATION, cid) is None
    (msg,) = web.get(f"/api/conversations/{cid}/messages").json()
    assert "expired after 1h" in msg["text"]


def test_archive_and_delete_drop_the_heartbeat(web):
    a, b = _conv(web), _conv(web)
    heartbeat.set_heartbeat(CONVERSATION, a, "x")
    heartbeat.set_heartbeat(CONVERSATION, b, "x")
    web.post(f"/api/conversations/{a}/archive")
    web.delete(f"/api/conversations/{b}")
    assert hs.store.list_all() == []


def test_user_turn_postpones_the_next_tick(web, monkeypatch):
    monkeypatch.setattr(
        conversation, "run_agent", lambda *a, **k: RunResult(text="ok", session_id="s")
    )
    cid = _conv(web)
    heartbeat.set_heartbeat(CONVERSATION, cid, "x", interval_s=60)
    _make_due(CONVERSATION, cid)
    platform = FakePlatform()
    conversation.handle_incoming_message(
        platform, IncomingMessage(platform="web", conversation_key=cid, message_id="m", text="hi")
    )
    assert hs.store.get(CONVERSATION, cid).next_due_at > time.time() + 50


# -- task ticks ------------------------------------------------------------------


class FakeProjects:
    def get_path(self, name):
        return "/repo"


class FakeHarness:
    name = "claude-code"
    label = "Fake"
    bin = "fake"
    missing_session_marker = None

    def session_exists(self, session_id, cwd):
        return True


@pytest.fixture
def board(monkeypatch):
    """A harvester running real turns against a scripted agent."""
    monkeypatch.setattr(harvester_mod, "HARNESSES", {"claude-code": object()})
    monkeypatch.setattr(runner, "get_harness", lambda name: FakeHarness())
    monkeypatch.setattr(runner, "ProjectStore", FakeProjects)

    class Board:
        script: list = []
        prompts: list[str] = []
        harvester = Harvester()

        def run(self):
            started = self.harvester.tick()
            self.harvester.join(5)
            return started

    def fake_run_agent(harness, prompt, **kwargs):
        Board.prompts.append(prompt)
        return Board.script.pop(0)(kwargs)

    Board.script, Board.prompts = [], []
    monkeypatch.setattr(runner, "run_agent", fake_run_agent)
    transitions.add_listener(heartbeat.on_task_transition)
    return Board()


def _waiting_task(status=READY_FOR_REVIEW):
    task = taskboard.store.create(title="Watch PR", project="p", harness="claude-code", use_worktree=False)
    claim(task.id)
    taskboard.store.update_fields(task.id, session_id="sess")
    transition(task.id, status, reason="turn finished")
    heartbeat.set_heartbeat(TASK, str(task.id), "check CI", interval_s=60)
    _make_due(TASK, task.id)
    return task


def _says(text, **kw):
    return lambda kwargs: RunResult(text=text, session_id="sess", **kw)


@pytest.mark.parametrize("start", [READY_FOR_REVIEW, NEEDS_INPUT])
def test_quiet_task_tick_returns_silently(board, start):
    task = _waiting_task(start)
    before = len(taskboard.store.comments(task.id))
    board.script.append(_says("CI still running <silence>"))
    assert board.run() == [task.id]
    t = taskboard.store.get(task.id)
    assert t.status == start and t.heartbeat_return_status is None
    assert len(taskboard.store.comments(task.id)) == before  # nothing on the timeline
    assert "check CI" in board.prompts[0]
    (tick,) = hs.store.ticks(TASK, str(task.id))
    assert tick.outcome == hs.QUIET and tick.response == "CI still running"


def test_notify_task_tick_moves_to_review(board):
    task = _waiting_task(NEEDS_INPUT)
    seen = []
    transitions.add_listener(seen.append)
    board.script.append(_says("Rebased, CI green <notify>"))
    board.run()
    assert taskboard.store.get(task.id).status == READY_FOR_REVIEW
    agent = [c.body for c in taskboard.store.comments(task.id) if c.author == "agent"]
    assert agent == ["Rebased, CI green"]
    assert seen[-1].to_status == READY_FOR_REVIEW and seen[-1].notify
    assert hs.store.get(TASK, str(task.id)) is not None  # keeps running


def test_task_tick_that_asks_moves_to_needs_input(board):
    task = _waiting_task(READY_FOR_REVIEW)

    def asks(kwargs):
        actions.ask(task.id, "Merge now?")
        return RunResult(text="Asked. <notify>", session_id="sess")

    board.script.append(asks)
    board.run()
    assert taskboard.store.get(task.id).status == NEEDS_INPUT


def test_task_tick_done_drops_the_heartbeat(board):
    task = _waiting_task()

    def done(kwargs):
        actions.mark_done(task.id)
        return RunResult(text="PR merged.", session_id="sess")

    board.script.append(done)
    board.run()
    assert taskboard.store.get(task.id).status == DONE
    assert hs.store.get(TASK, str(task.id)) is None


def test_running_or_queued_task_waits(board):
    task = _waiting_task()
    transition(task.id, TODO, reason="new comment")
    board.script.append(_says("normal turn"))
    board.run()  # the To Do pickup is an ordinary turn
    assert board.prompts and "check CI" not in board.prompts[0]
    # The turn's end counted as a check: not due again yet.
    assert heartbeat.due_tasks() == []


def test_task_ticks_respect_the_pause_switch(board):
    _waiting_task()
    taskboard.board_state.set_paused(True)
    assert board.run() == []


def test_recover_returns_an_interrupted_tick(board):
    task = _waiting_task(NEEDS_INPUT)
    assert transitions.claim_heartbeat(task.id) is not None
    assert taskboard.store.get(task.id).status == IN_PROGRESS
    board.harvester.recover()
    assert taskboard.store.get(task.id).status == NEEDS_INPUT


def test_claim_heartbeat_is_quiet():
    task = taskboard.store.create(title="t", project="p", harness="claude-code", use_worktree=False)
    claim(task.id)
    transition(task.id, READY_FOR_REVIEW, reason="turn finished")
    before = len(taskboard.store.comments(task.id))
    assert transitions.claim_heartbeat(task.id).heartbeat_return_status == READY_FOR_REVIEW
    assert len(taskboard.store.comments(task.id)) == before
    transition(task.id, READY_FOR_REVIEW, reason="heartbeat", quiet=True)
    assert taskboard.store.get(task.id).heartbeat_return_status is None
    transition(task.id, TODO, reason="x")
    assert transitions.claim_heartbeat(task.id) is None  # only waiting tasks


@pytest.fixture
def task_api(monkeypatch):
    monkeypatch.setattr(actions, "ProjectStore", FakeProjects)
    monkeypatch.setattr(actions, "HARNESSES", {"claude-code": object()})
    monkeypatch.setattr(routes, "HARNESSES", {"claude-code": object()})
    monkeypatch.setattr(actions, "default_harness", lambda: "claude-code")
    monkeypatch.setattr(taskboard, "active_harvester", None)
    return TestClient(web_server.create_api())


def test_task_created_with_a_heartbeat(task_api):
    r = task_api.post("/api/tasks", json={
        "title": "Babysit PR", "project": "proj",
        "heartbeat_prompt": "watch the PR", "heartbeat_interval_s": 600,
    })
    assert r.status_code == 201, r.text
    hb = r.json()["heartbeat"]
    assert hb["prompt"] == "watch the PR" and hb["interval_s"] == 600
    assert task_api.get("/api/tasks").json()[0]["heartbeat"]["interval_s"] == 600
    bad = task_api.post("/api/tasks", json={
        "title": "x", "project": "proj", "heartbeat_prompt": "x", "heartbeat_interval_s": 5,
    })
    assert bad.status_code == 400
    assert len(task_api.get("/api/tasks").json()) == 1  # no task without its heartbeat


def test_archive_and_done_task_heartbeats(task_api):
    task = _waiting_task(NEEDS_INPUT)
    actions.archive(task.id)
    assert hs.store.get(TASK, str(task.id)) is None
    other = _waiting_task(NEEDS_INPUT)
    assert task_api.put(f"/api/heartbeats/task/{other.id}", json={"prompt": "x"}).status_code == 200
    transitions.add_listener(heartbeat.on_task_transition)
    actions.mark_done(other.id)
    assert hs.store.get(TASK, str(other.id)) is None
    assert task_api.put(f"/api/heartbeats/task/{other.id}", json={"prompt": "x"}).status_code == 409


# -- CLI -------------------------------------------------------------------------


@pytest.fixture
def cli(monkeypatch, web):
    sent = []

    def fake_request(method, path, payload=None, *, what="Request"):
        sent.append((method, path, payload))
        resp = web.request(method, path, json=payload)
        if resp.status_code >= 400:
            print(f"{what} failed ({resp.status_code}): {resp.json()['detail']}",
                  file=__import__("sys").stderr)
            raise SystemExit(1)
        return resp.json()

    monkeypatch.setattr(cli_heartbeat, "api_request", fake_request)
    monkeypatch.delenv("YUKI_TASK_ID", raising=False)
    monkeypatch.delenv("YUKI_CONVERSATION_ID", raising=False)
    return sent


def test_cli_targets_the_runs_conversation(cli, web, monkeypatch, capsys):
    cid = _conv(web)
    monkeypatch.setenv("YUKI_CONVERSATION_ID", cid)
    main(["heartbeat", "set", "--every", "60m", "--for", "3d", "check the grant"])
    out = capsys.readouterr()
    assert "every 1h" in out.out and out.err == ""
    hb = hs.store.get(CONVERSATION, cid)
    assert hb.interval_s == 3600 and hb.created_by == "agent"
    assert hb.expires_at == pytest.approx(hb.created_at + 3 * 86400)
    main(["heartbeat", "pause"])
    assert hs.store.get(CONVERSATION, cid).paused
    main(["heartbeat", "status"])
    assert "paused" in capsys.readouterr().out
    main(["heartbeat", "stop"])
    assert hs.store.get(CONVERSATION, cid) is None


def test_cli_default_interval_and_task_target(cli, monkeypatch, capsys):
    task = _waiting_task()
    hs.store.delete(TASK, str(task.id))
    monkeypatch.setenv("YUKI_TASK_ID", str(task.id))
    monkeypatch.setenv("YUKI_CONVERSATION_ID", "ignored")
    main(["heartbeat", "set", "watch it"])
    assert hs.store.get(TASK, str(task.id)).interval_s == 270
    assert cli[-1][1] == f"/api/heartbeats/task/{task.id}"


def test_cli_warns_on_cache_cold_intervals(cli, web, capsys):
    cid = _conv(web)
    main(["heartbeat", "set", "-c", cid, "--every", "10m", "x"])
    assert "prompt cache" in capsys.readouterr().err


def test_cli_rejects_more_than_seven_days(cli, web, capsys):
    cid = _conv(web)
    with pytest.raises(SystemExit):
        main(["heartbeat", "set", "-c", cid, "--for", "8d", "x"])
    assert "at most 7 days" in capsys.readouterr().err


def test_cli_needs_a_target(cli, capsys):
    with pytest.raises(SystemExit):
        main(["heartbeat", "status"])
    assert "No target" in capsys.readouterr().err

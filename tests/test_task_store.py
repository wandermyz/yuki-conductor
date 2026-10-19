"""TaskStore: tasks, queue ordering, comments, archive cutoff."""

import sqlite3
import threading
import time

import pytest

from yuki_conductor.task_store import (
    ARCHIVE_AFTER_SECONDS,
    DONE,
    IN_PROGRESS,
    TODO,
    TaskboardStateStore,
    TaskStore,
)


@pytest.fixture
def store(tmp_path):
    return TaskStore(db_path=tmp_path / "t.db")


def _new(store, title="t", **kw):
    kw.setdefault("project", "proj")
    kw.setdefault("harness", "claude-code")
    kw.setdefault("use_worktree", True)
    return store.create(title=title, **kw)


def _todo_titles(store):
    return [t.title for t in store.list_status(TODO)]


def test_create_roundtrip(store):
    task = _new(store, "Fix it", body="**md**", attachments=["/tmp/a.png"], use_worktree=False)
    got = store.get(task.id)
    assert got.title == "Fix it"
    assert got.body == "**md**"
    assert got.attachments == ["/tmp/a.png"]
    assert got.use_worktree is False
    assert got.status == TODO
    assert got.started_at is None
    assert got.editable


def test_get_missing_returns_none(store):
    assert store.get(999) is None


def test_new_tasks_append_to_queue(store):
    for title in "abc":
        _new(store, title)
    assert _todo_titles(store) == ["a", "b", "c"]


def test_update_fields_whitelist(store):
    task = _new(store)
    updated = store.update_fields(task.id, title="new", asked_input=True, attachments=["/x"])
    assert updated.title == "new"
    assert updated.asked_input is True
    assert updated.attachments == ["/x"]
    with pytest.raises(ValueError):
        store.update_fields(task.id, status=DONE)


def test_claim_once(store):
    task = _new(store)
    assert store.claim(task.id)
    assert not store.claim(task.id)
    claimed = store.get(task.id)
    assert claimed.status == IN_PROGRESS
    assert claimed.started_at is not None
    assert not claimed.editable


def test_claim_race_has_one_winner(store):
    task = _new(store)
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(store.claim(task.id))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_claim_keeps_first_started_at(store):
    task = _new(store)
    store.claim(task.id)
    first = store.get(task.id).started_at
    store.set_status(task.id, TODO, expected=IN_PROGRESS, position="end")
    store.claim(task.id)
    assert store.get(task.id).started_at == first


def test_set_status_top_and_end(store):
    a, b, c = (_new(store, t) for t in "abc")
    store.claim(c.id)
    store.set_status(c.id, TODO, expected=IN_PROGRESS, position="top")
    assert _todo_titles(store) == ["c", "a", "b"]
    store.claim(c.id)
    store.set_status(c.id, TODO, expected=IN_PROGRESS, position="end")
    assert _todo_titles(store) == ["a", "b", "c"]


def test_set_status_done_sets_done_at(store):
    task = _new(store)
    assert store.set_status(task.id, DONE, expected=TODO)
    assert store.get(task.id).done_at is not None


def test_set_status_rejects_unknown(store):
    task = _new(store)
    with pytest.raises(ValueError):
        store.set_status(task.id, "cancelled", expected=TODO)


def test_set_status_is_compare_and_set(store):
    task = _new(store)
    store.claim(task.id)
    assert not store.set_status(task.id, DONE, expected=TODO)
    assert store.get(task.id).status == IN_PROGRESS


def test_move_to_top_and_end(store):
    a, b, c = (_new(store, t) for t in "abc")
    store.move_to_top(c.id)
    assert _todo_titles(store) == ["c", "a", "b"]
    store.move_to_end(c.id)
    assert _todo_titles(store) == ["a", "b", "c"]


def test_reorder_keeps_unnamed_tasks_in_place(store):
    a, b, c, d = (_new(store, t) for t in "abcd")
    store.reorder([d.id, b.id])  # swap b and d; a and c stay put
    assert _todo_titles(store) == ["a", "d", "c", "b"]


def test_reorder_rejects_non_todo(store):
    a, b = _new(store, "a"), _new(store, "b")
    store.claim(b.id)
    with pytest.raises(ValueError):
        store.reorder([b.id, a.id])
    with pytest.raises(ValueError):
        store.reorder([a.id, a.id])


def test_list_hides_archived_done(store, monkeypatch):
    old, recent = _new(store, "old"), _new(store, "recent")
    _new(store, "open")
    store.set_status(old.id, DONE, expected=TODO)
    import yuki_conductor.task_store as ts

    real_time = time.time()
    monkeypatch.setattr(ts.time, "time", lambda: real_time + ARCHIVE_AFTER_SECONDS + 10)
    store.set_status(recent.id, DONE, expected=TODO)  # done "now", i.e. 30 days after `old`
    assert [t.title for t in store.list_tasks()] == ["recent", "open"]
    assert [t.title for t in store.list_tasks(include_archived=True)] == ["old", "recent", "open"]


def test_children(store):
    parent = _new(store, "parent")
    child = _new(store, "child", parent_task_id=parent.id)
    assert [t.id for t in store.children(parent.id)] == [child.id]


def test_running_counts(store):
    tasks = [_new(store, harness=h) for h in ("claude-code", "claude-code", "pi", "pi")]
    for t in tasks[:3]:
        store.claim(t.id)
    assert store.running_counts() == {"claude-code": 2, "pi": 1}


def test_comments_and_delivery(store):
    task = _new(store)
    user = store.add_comment(task.id, "user", "please", attachments=["/f"])
    agent = store.add_comment(task.id, "agent", "ok", quote_comment_id=user.id)
    gh = store.add_comment(
        task.id, "github", "nit", external_url="https://x", external_author="octo"
    )
    store.add_comment(task.id, "system", "moved")
    assert not user.delivered and agent.delivered
    assert [c.id for c in store.undelivered(task.id)] == [user.id, gh.id]
    store.mark_delivered([user.id])
    assert [c.id for c in store.undelivered(task.id)] == [gh.id]
    timeline = store.comments(task.id)
    assert [c.author for c in timeline] == ["user", "agent", "github", "system"]
    assert timeline[0].attachments == ["/f"]
    assert timeline[1].quote_comment_id == user.id
    assert timeline[2].external_author == "octo"


def test_comment_bumps_updated_at(store):
    task = _new(store)
    store.add_comment(task.id, "user", "hi")
    assert store.get(task.id).updated_at >= task.updated_at


def test_comment_rejects_unknown_author(store):
    task = _new(store)
    with pytest.raises(ValueError):
        store.add_comment(task.id, "bot", "hi")


def test_comment_on_missing_task(store):
    with pytest.raises(KeyError):
        store.add_comment(404, "user", "hi")


def test_pause_state(tmp_path):
    state = TaskboardStateStore(db_path=tmp_path / "t.db")
    assert not state.paused()
    state.set_paused(True)
    assert TaskboardStateStore(db_path=tmp_path / "t.db").paused()
    state.set_paused(False)
    assert not state.paused()


def test_pr_state_column_is_added_and_backfilled(tmp_path):
    db = tmp_path / "t.db"
    store = TaskStore(db_path=db)
    merged, closed, open_ = (_new(store, title=t) for t in ("m", "c", "o"))
    for task in (merged, closed, open_):
        store.update_fields(task.id, pr_url=f"https://github.com/o/r/pull/{task.id}")
    store.add_comment(merged.id, "system", "Ready for Review → Done: PR merged")
    store.add_comment(closed.id, "system", "Ready for Review → Done: PR closed without merging")
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE tasks DROP COLUMN pr_state")
    con.commit()
    con.close()

    store = TaskStore(db_path=db)
    assert [store.get(t.id).pr_state for t in (merged, closed, open_)] == [
        "MERGED", "CLOSED", None,
    ]

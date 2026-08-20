"""Tests for CronRunStore — the cron run history in the workspace DB."""

from yuki_conductor.store import CronRunStore


def _store(tmp_path):
    return CronRunStore(db_path=tmp_path / "test.db")


def test_start_and_finish_roundtrip(tmp_path):
    store = _store(tmp_path)
    run_id = store.start_run("task-a")

    running = store.list_runs("task-a")
    assert len(running) == 1
    assert running[0]["status"] == "running"
    assert running[0]["trigger"] == "schedule"
    assert running[0]["finished_at"] is None

    store.finish_run(
        run_id,
        status="success",
        response="all good",
        notified=True,
        session_id="sess-1",
        conversation_id="conv-1",
    )
    done = store.list_runs("task-a")[0]
    assert done["status"] == "success"
    assert done["response"] == "all good"
    assert done["notified"] is True
    assert done["session_id"] == "sess-1"
    assert done["conversation_id"] == "conv-1"
    assert done["finished_at"] >= done["started_at"]


def test_finish_run_records_error(tmp_path):
    store = _store(tmp_path)
    store.finish_run(store.start_run("t"), status="error", error="boom")
    run = store.list_runs("t")[0]
    assert run["status"] == "error"
    assert run["error"] == "boom"
    assert run["response"] is None


def test_manual_trigger_is_recorded(tmp_path):
    store = _store(tmp_path)
    store.start_run("t", trigger="manual")
    assert store.list_runs("t")[0]["trigger"] == "manual"


def test_history_capped_per_task(tmp_path):
    store = _store(tmp_path)
    for _ in range(CronRunStore.MAX_RUNS_PER_TASK + 12):
        store.finish_run(store.start_run("t"), status="success", response="x")
    assert len(store.list_runs("t", limit=100)) == CronRunStore.MAX_RUNS_PER_TASK


def test_tasks_have_independent_history(tmp_path):
    store = _store(tmp_path)
    store.start_run("a")
    store.start_run("b")
    store.start_run("b")
    assert len(store.list_runs("a")) == 1
    assert len(store.list_runs("b")) == 2


def test_last_run_is_newest(tmp_path):
    store = _store(tmp_path)
    store.finish_run(store.start_run("t"), status="success", response="old")
    second = store.start_run("t")
    store.finish_run(second, status="success", response="new")
    assert store.last_run("t")["response"] == "new"
    assert store.last_run("missing") is None


def test_is_running_tracks_in_flight_runs(tmp_path):
    store = _store(tmp_path)
    assert not store.is_running("t")
    run_id = store.start_run("t")
    assert store.is_running("t")
    store.finish_run(run_id, status="success", response="done")
    assert not store.is_running("t")

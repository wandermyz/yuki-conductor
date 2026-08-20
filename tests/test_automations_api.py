"""Tests for the /api/automations endpoints."""

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import web_server
from yuki_conductor.store import CronRunStore

CRON_YAML = """\
tasks:
  - name: briefing
    schedule: "0 9 * * 1-5"
    description: "Morning briefing"
    prompt: >
      Give me a briefing.
    origin_conversation: conv-abc

  - name: weekly
    display_name: "Weekly check"
    schedule: "30 10 * * 1"
    prompt: "Check deps."
"""


@pytest.fixture
def cron_file(monkeypatch, tmp_path):
    path = tmp_path / "cron.yaml"
    path.write_text(CRON_YAML, encoding="utf-8")
    monkeypatch.setattr("yuki_conductor.cron_config.CRON_FILE", path)
    return path


@pytest.fixture
def runs(monkeypatch, tmp_path):
    store = CronRunStore(db_path=tmp_path / "yuki.db")
    monkeypatch.setattr(web_server, "cron_run_store", store)
    return store


@pytest.fixture
def client(cron_file, runs):
    return TestClient(web_server.create_api())


def test_list_automations_exposes_labels_and_cadence(client):
    items = client.get("/api/automations").json()
    assert [a["name"] for a in items] == ["briefing", "weekly"]

    briefing = items[0]
    assert briefing["label"] == "Morning briefing"
    assert briefing["schedule"] == "0 9 * * 1-5"
    assert briefing["schedule_text"] == "at 09:00 on Monday through Friday"
    assert len(briefing["next_runs"]) == 3
    assert briefing["origin_conversation"] == "conv-abc"
    assert briefing["running"] is False

    # display_name wins over description as the label.
    assert items[1]["label"] == "Weekly check"


def test_list_automations_omits_run_history(client):
    assert "runs" not in client.get("/api/automations").json()[0]


def test_list_automations_sorted_by_last_run(client, runs):
    # "weekly" is second in the file but ran most recently, so it sorts first;
    # a never-run task sinks below both.
    runs.finish_run(runs.start_run("briefing"), status="success")
    runs.finish_run(runs.start_run("weekly"), status="success")

    assert [a["name"] for a in client.get("/api/automations").json()] == [
        "weekly",
        "briefing",
    ]


def test_get_automation_includes_prompt_and_runs(client, runs):
    runs.finish_run(runs.start_run("briefing"), status="success", response="hello")

    data = client.get("/api/automations/briefing").json()
    assert "Give me a briefing." in data["prompt"]
    assert len(data["runs"]) == 1
    assert data["runs"][0]["response"] == "hello"
    assert data["last_run"]["response"] == "hello"


def test_get_automation_404(client):
    assert client.get("/api/automations/nope").status_code == 404


def test_rename_automation_persists_to_yaml(client, cron_file):
    r = client.patch("/api/automations/briefing", json={"display_name": "Daily brief"})
    assert r.status_code == 200
    assert r.json()["label"] == "Daily brief"
    assert 'display_name: "Daily brief"' in cron_file.read_text(encoding="utf-8")
    # Survives a reload through the list endpoint.
    assert client.get("/api/automations").json()[0]["label"] == "Daily brief"


def test_rename_to_blank_restores_derived_label(client):
    client.patch("/api/automations/weekly", json={"display_name": "  "})
    assert client.get("/api/automations").json()[1]["label"] == "Check deps."


def test_rename_unknown_automation_404(client):
    r = client.patch("/api/automations/nope", json={"display_name": "x"})
    assert r.status_code == 404


def test_run_now_triggers_the_task(client, monkeypatch):
    called = []
    monkeypatch.setattr(
        "yuki_conductor.cron_scheduler.trigger_task",
        lambda name: called.append(name) or True,
    )
    assert client.post("/api/automations/briefing/run").status_code == 200
    assert called == ["briefing"]


def test_run_now_404_for_unknown_task(client, monkeypatch):
    monkeypatch.setattr("yuki_conductor.cron_scheduler.trigger_task", lambda name: False)
    assert client.post("/api/automations/nope/run").status_code == 404


def test_run_now_conflicts_while_already_running(client, runs, monkeypatch):
    runs.start_run("briefing")
    monkeypatch.setattr(
        "yuki_conductor.cron_scheduler.trigger_task",
        lambda name: pytest.fail("should not trigger a second run"),
    )
    assert client.post("/api/automations/briefing/run").status_code == 409


def test_runs_endpoint_caps_at_thirty(client, runs):
    for _ in range(35):
        runs.finish_run(runs.start_run("briefing"), status="success", response="x")
    assert len(client.get("/api/automations/briefing/runs").json()) == 30
    assert client.get("/api/automations/briefing/runs?limit=99").status_code == 422

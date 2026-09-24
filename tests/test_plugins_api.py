"""Tests for the /api/plugins and /api/daemon endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import web_server
from yuki_conductor.plugin_config import ensure_file, load_records


@pytest.fixture
def client(isolated_plugin_registry):
    ensure_file()
    web_server._clear_restart_required()
    return TestClient(web_server.create_api())


def _echo_plugin(tmp_path):
    d = tmp_path / "echo-plugin"
    d.mkdir()
    (d / "yuki-plugin.yaml").write_text(
        "name: echo\ndescription: Example\nversion: 0.1.0\n", encoding="utf-8"
    )
    return d


def test_list_includes_bundled_slack(client):
    body = client.get("/api/plugins").json()
    names = [p["name"] for p in body["plugins"]]
    assert "slack" in names
    slack = next(p for p in body["plugins"] if p["name"] == "slack")
    assert slack["builtin"] is True
    assert slack["channels"] == ["slack"]
    assert slack["status"] == "ok", slack["error"]


def test_list_reports_override(client, monkeypatch):
    assert client.get("/api/plugins").json()["channels_override"] is None
    monkeypatch.setenv("CHANNELS", "none")
    assert client.get("/api/plugins").json()["channels_override"] == []


def test_enable_toggles_registry_and_flags_restart(client):
    resp = client.patch("/api/plugins/slack", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["restart_required"] is True
    assert load_records()[0].enabled is True
    assert client.get("/api/plugins").json()["restart_required"] is True


def test_enable_unknown_plugin_404s(client):
    assert client.patch("/api/plugins/nope", json={"enabled": True}).status_code == 404


def test_add_plugin_by_path(client, tmp_path):
    resp = client.post("/api/plugins", json={"path": str(_echo_plugin(tmp_path))})
    assert resp.status_code == 201
    assert resp.json()["name"] == "echo"
    body = client.get("/api/plugins").json()
    echo = next(p for p in body["plugins"] if p["name"] == "echo")
    assert echo["enabled"] is False  # registered disabled
    assert echo["source"] == "path"


def test_add_rejects_non_directory(client, tmp_path):
    resp = client.post("/api/plugins", json={"path": str(tmp_path / "nope")})
    assert resp.status_code == 400
    assert "Not a directory" in resp.json()["detail"]


def test_add_rejects_dir_without_manifest(client, tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    resp = client.post("/api/plugins", json={"path": str(d)})
    assert resp.status_code == 400
    assert "yuki-plugin.yaml" in resp.json()["detail"]


def test_add_rejects_duplicate(client, tmp_path):
    path = str(_echo_plugin(tmp_path))
    assert client.post("/api/plugins", json={"path": path}).status_code == 201
    assert client.post("/api/plugins", json={"path": path}).status_code == 409


def test_delete_removes_path_plugin(client, tmp_path):
    client.post("/api/plugins", json={"path": str(_echo_plugin(tmp_path))})
    assert client.delete("/api/plugins/echo").status_code == 200
    assert [r.name for r in load_records()] == ["slack"]


def test_delete_builtin_is_rejected(client):
    resp = client.delete("/api/plugins/slack")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["detail"]
    assert [r.name for r in load_records()] == ["slack"]


def test_delete_unknown_404s(client):
    assert client.delete("/api/plugins/nope").status_code == 404


def test_restart_spawns_detached_and_clears_flag(client):
    client.patch("/api/plugins/slack", json={"enabled": True})
    with patch("yuki_conductor.daemon.spawn_detached_restart") as spawn:
        resp = client.post("/api/daemon/restart")
    assert resp.status_code == 202
    spawn.assert_called_once_with()
    assert client.get("/api/plugins").json()["restart_required"] is False


def test_restart_failure_is_reported(client):
    with patch(
        "yuki_conductor.daemon.spawn_detached_restart",
        side_effect=RuntimeError("no script"),
    ):
        resp = client.post("/api/daemon/restart")
    assert resp.status_code == 500
    assert "no script" in resp.json()["detail"]

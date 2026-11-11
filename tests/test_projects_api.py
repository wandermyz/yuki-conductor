"""Tests for the /api/projects endpoints."""

from fastapi.testclient import TestClient

from yuki_conductor import web_server


def test_default_reports_the_working_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(web_server, "CLAUDE_WORKING_DIR", str(tmp_path))
    client = TestClient(web_server.create_api())
    assert client.get("/api/projects/default").json() == {"path": str(tmp_path)}

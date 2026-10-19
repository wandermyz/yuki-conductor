"""web/dist is built per machine; the daemon rebuilds it when the sources move on."""

import os
import threading
from unittest.mock import patch

import pytest

from yuki_conductor import config

# Bound at import, before conftest's ``no_web_build`` swaps the module attribute.
from yuki_conductor.config import web_frontend_stale


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "project_dir", lambda: tmp_path)
    web = tmp_path / "web"
    (web / "src" / "tasks").mkdir(parents=True)
    (web / "src" / "tasks" / "TaskForm.tsx").write_text("x")
    (web / "package.json").write_text("{}")
    return web


def _build(web, mtime):
    built = web / "dist" / "index.html"
    built.parent.mkdir(exist_ok=True)
    built.write_text("<html>")
    os.utime(built, (mtime, mtime))


def _touch(path, mtime):
    os.utime(path, (mtime, mtime))


def test_missing_dist_is_stale(web):
    assert web_frontend_stale() is True


def test_dist_newer_than_every_source_is_fresh(web):
    for path in web.rglob("*"):
        _touch(path, 1000)
    _build(web, 2000)
    assert web_frontend_stale() is False


def test_nested_source_newer_than_dist_is_stale(web):
    _touch(web / "package.json", 1000)
    _build(web, 2000)
    _touch(web / "src" / "tasks" / "TaskForm.tsx", 3000)
    assert web_frontend_stale() is True


def test_unrelated_files_are_ignored(web):
    for path in web.rglob("*"):
        _touch(path, 1000)
    _build(web, 2000)
    (web / "node_modules").mkdir()
    _touch(web / "node_modules", 3000)
    (web / "README.md").write_text("x")
    assert web_frontend_stale() is False


@pytest.mark.parametrize("stale", [True, False])
def test_runtime_builds_only_when_stale(stale):
    with (
        patch.object(threading.Event, "wait", lambda self, timeout=None: None),
        patch("yuki_conductor.runtime.channels", return_value=[]),
        patch("yuki_conductor.plugin_config.ensure_file"),
        patch("yuki_conductor.config.web_frontend_stale", return_value=stale),
        patch("yuki_conductor.config.build_web_frontend", return_value=True) as build,
        patch("yuki_conductor.web_server.start_web_server"),
        patch("yuki_conductor.cron_scheduler.start_cron_scheduler"),
        patch("yuki_conductor.taskboard.start_taskboard"),
    ):
        from yuki_conductor.runtime import start

        start()

    assert build.called is stale

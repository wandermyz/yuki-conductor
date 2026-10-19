"""Self-update against a real temp checkout with a bare origin, plus /api/update."""

import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from yuki_conductor import updater, web_server
from yuki_conductor.updater import UpdateError

IDENT = ["-c", "user.email=t@t", "-c", "user.name=t"]


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *IDENT, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(cwd, path, content, message):
    (cwd / path).parent.mkdir(parents=True, exist_ok=True)
    (cwd / path).write_text(content)
    git(cwd, "add", path)
    git(cwd, "commit", "-m", message)


@pytest.fixture
def upstream(tmp_path):
    """(seed, checkout): push to `seed`, update `checkout`."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    commit(seed, "README", "one\n", "init")
    git(seed, "push", "origin", "HEAD:main")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(origin), str(checkout)], check=True, capture_output=True)
    return seed, checkout


def test_check_up_to_date(upstream):
    _, checkout = upstream
    status = updater.check(checkout)
    assert (status.branch, status.upstream) == ("main", "origin/main")
    assert status.behind == 0 and status.commits == []
    assert status.current == status.latest


def test_check_lists_new_commits(upstream):
    seed, checkout = upstream
    commit(seed, "a.txt", "a", "Add a")
    commit(seed, "b.txt", "b", "Add b")
    git(seed, "push", "origin", "HEAD:main")

    status = updater.check(checkout)
    assert status.behind == 2
    assert [c.subject for c in status.commits] == ["Add b", "Add a"]
    assert status.latest == git(seed, "rev-parse", "HEAD")


def test_apply_rebases_local_commits(upstream):
    seed, checkout = upstream
    commit(seed, "a.txt", "a", "Add a")
    git(seed, "push", "origin", "HEAD:main")
    commit(checkout, "local.txt", "l", "Local")

    built = []
    status = updater.apply(checkout, build_frontend=lambda: built.append(1) or True)
    assert (status.behind, status.ahead) == (1, 1)
    assert (checkout / "a.txt").exists() and (checkout / "local.txt").exists()
    assert git(checkout, "rev-parse", "HEAD~1") == git(seed, "rev-parse", "HEAD")
    assert built == []  # web/ untouched


def test_apply_rebuilds_frontend_when_web_changes(upstream):
    seed, checkout = upstream
    commit(seed, "web/src/x.ts", "x", "Web")
    git(seed, "push", "origin", "HEAD:main")
    built = []
    updater.apply(checkout, build_frontend=lambda: built.append(1) or True)
    assert built == [1]


def test_apply_fails_when_frontend_build_fails(upstream):
    seed, checkout = upstream
    commit(seed, "web/src/x.ts", "x", "Web")
    git(seed, "push", "origin", "HEAD:main")
    with pytest.raises(UpdateError, match="frontend build failed"):
        updater.apply(checkout, build_frontend=lambda: False)


def test_apply_refuses_local_changes(upstream):
    seed, checkout = upstream
    commit(seed, "a.txt", "a", "Add a")
    git(seed, "push", "origin", "HEAD:main")
    (checkout / "README").write_text("edited\n")
    before = git(checkout, "rev-parse", "HEAD")

    with pytest.raises(UpdateError, match="Local changes.*README"):
        updater.apply(checkout)
    assert git(checkout, "rev-parse", "HEAD") == before
    assert updater.check(checkout).dirty == ["README"]


def test_apply_ignores_untracked_files(upstream):
    seed, checkout = upstream
    commit(seed, "a.txt", "a", "Add a")
    git(seed, "push", "origin", "HEAD:main")
    (checkout / "scratch.txt").write_text("x")
    assert updater.apply(checkout).behind == 1


def test_apply_aborts_conflicting_rebase(upstream):
    seed, checkout = upstream
    commit(seed, "README", "theirs\n", "Theirs")
    git(seed, "push", "origin", "HEAD:main")
    commit(checkout, "README", "mine\n", "Mine")
    before = git(checkout, "rev-parse", "HEAD")

    with pytest.raises(UpdateError, match="Rebase failed and was aborted"):
        updater.apply(checkout)
    assert git(checkout, "rev-parse", "HEAD") == before
    assert git(checkout, "status", "--porcelain") == ""


def test_no_upstream_is_an_error(upstream):
    _, checkout = upstream
    git(checkout, "checkout", "-b", "local-only")
    with pytest.raises(UpdateError, match="no upstream"):
        updater.check(checkout)


# ---------------------------------------------------------------- the API


@pytest.fixture
def client(upstream, monkeypatch):
    _, checkout = upstream
    monkeypatch.setattr(updater, "project_dir", lambda: checkout)
    monkeypatch.setattr(updater, "build_web_frontend", lambda: True)
    return TestClient(web_server.create_api())


def test_api_check(client, upstream):
    seed, _ = upstream
    commit(seed, "a.txt", "a", "Add a")
    git(seed, "push", "origin", "HEAD:main")
    body = client.get("/api/update").json()
    assert body["behind"] == 1
    assert body["commits"][0]["subject"] == "Add a"
    assert body["running"] == {}


def test_api_check_reports_runs_in_flight(client):
    with patch(
        "yuki_conductor.harness.process.running_runs",
        return_value={"chat": 1, "task": 2},
    ):
        body = client.get("/api/update").json()
    assert body["running"] == {"chat": 1, "task": 2}


def test_api_apply_up_to_date_does_not_restart(client):
    with patch("yuki_conductor.daemon.spawn_detached_restart") as restart:
        resp = client.post("/api/update")
    assert resp.status_code == 202
    assert resp.json()["restarting"] is False
    restart.assert_not_called()


def test_api_apply_restarts(client, upstream):
    seed, checkout = upstream
    commit(seed, "a.txt", "a", "Add a")
    git(seed, "push", "origin", "HEAD:main")
    with patch("yuki_conductor.daemon.spawn_detached_restart") as restart:
        resp = client.post("/api/update")
    assert resp.status_code == 202
    assert resp.json()["restarting"] is True
    restart.assert_called_once()
    assert (checkout / "a.txt").exists()


def test_api_apply_local_changes_409(client, upstream):
    _, checkout = upstream
    (checkout / "README").write_text("edited\n")
    with patch("yuki_conductor.daemon.spawn_detached_restart") as restart:
        resp = client.post("/api/update")
    assert resp.status_code == 409
    assert "Local changes" in resp.json()["detail"]
    restart.assert_not_called()

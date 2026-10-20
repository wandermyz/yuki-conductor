"""Shared test fixtures."""

import os
import tempfile

# Before anything imports `config`: modules that build a store at import time
# (`web_server`, `taskboard`) would otherwise open the real workspace DB, and
# a store's startup work, like converting a search index to a new format,
# would land on the database the running daemon uses.
os.environ["YUKI_CONDUCTOR_DATA_DIR"] = tempfile.mkdtemp(prefix="yuki-conductor-tests-")

import pytest  # noqa: E402

from yuki_conductor.store import CronRunStore  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cron_run_store(monkeypatch, tmp_path):
    """Keep cron run history out of the real workspace database.

    ``cron_scheduler`` records every firing through a module-level store bound
    to the real DB at import time, so any test that exercises a cron run would
    otherwise append rows to the user's actual history.
    """
    from yuki_conductor import cron_scheduler

    store = CronRunStore(db_path=tmp_path / "cron-runs.db")
    monkeypatch.setattr(cron_scheduler, "run_store", store)
    return store


@pytest.fixture(autouse=True)
def isolated_plugin_registry(monkeypatch, tmp_path):
    """Keep the plugin registry out of the real workspace.

    ``plugin_config`` defaults every path argument to the real
    ``workspace/plugins.yaml``, so a test that enables or registers a plugin
    would otherwise edit the user's live daemon config.
    """
    from yuki_conductor import plugin_config

    path = tmp_path / "plugins.yaml"
    monkeypatch.setattr(plugin_config, "PLUGINS_FILE", path)
    # The env override must not leak in from the developer's shell either.
    monkeypatch.delenv("CHANNELS", raising=False)
    return path


@pytest.fixture(autouse=True)
def isolated_push_subscriptions(monkeypatch, tmp_path):
    """Keep browser push out of the real workspace.

    Every web assistant message calls ``web_push.notify``, which reads the
    module-level subscription store; bound to the real DB, a test reply would
    send a real notification to the user's browsers.
    """
    from yuki_conductor import web_push
    from yuki_conductor.web_push import PushSubscriptionStore

    store = PushSubscriptionStore(db_path=tmp_path / "push.db")
    monkeypatch.setattr(web_push, "subscriptions", store)
    monkeypatch.setattr(web_push, "VAPID_KEY_FILE", tmp_path / "vapid-private.pem")
    monkeypatch.setattr(web_push, "_vapid", None)
    return store


@pytest.fixture(autouse=True)
def isolated_task_store(monkeypatch, tmp_path):
    """Keep taskboard tasks and the pause switch out of the real workspace.

    ``taskboard`` binds its stores to the real DB at import time, and every
    part of the board reads them through the package, so rebinding here
    covers all of it.
    """
    from yuki_conductor import taskboard
    from yuki_conductor.task_store import TaskboardStateStore, TaskStore
    from yuki_conductor.taskboard import transitions

    store = TaskStore(db_path=tmp_path / "tasks.db")
    monkeypatch.setattr(taskboard, "store", store)
    monkeypatch.setattr(taskboard, "board_state", TaskboardStateStore(db_path=tmp_path / "tasks.db"))
    # Listeners registered at import (web broadcast, notify, cleanup) must
    # not fire on a test's transitions; tests that want one add it.
    monkeypatch.setattr(transitions, "_listeners", [])
    return store


@pytest.fixture(autouse=True)
def no_stray_harvester():
    """Fail a test that leaves a real taskboard loop running.

    The loop reads ``taskboard.store`` on every tick, so one leaked from a
    test (e.g. a ``runtime.start()`` without ``start_taskboard`` patched)
    claims tasks in every later test's store.
    """
    import threading

    yield
    loops = ("taskboard-harvester", "taskboard-pr-watcher")
    leaked = [t.name for t in threading.enumerate() if t.name in loops]
    assert not leaked, "a test started the real taskboard loop; patch start_taskboard"


@pytest.fixture(autouse=True)
def no_web_build(monkeypatch):
    """Stop ``runtime.start()`` from running a real pnpm build.

    It builds web/dist whenever the checkout's is missing or stale, which in a
    fresh worktree is always; tests that care patch the check themselves.
    """
    from yuki_conductor import config

    monkeypatch.setattr(config, "web_frontend_stale", lambda: False)

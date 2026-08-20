"""Shared test fixtures."""

import pytest

from yuki_conductor.store import CronRunStore


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

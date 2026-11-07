import json
import time
from pathlib import Path

import pytest

from reqreview.events import Dispatcher, EventLog, RequirementWatcher
from reqreview.runner import ReplayRunner
from reqreview.store import Store

DOC = "REQUIREMENT: Payment Retry\n\nFailed payments should be retried appropriately.\n\nStop after a reasonable number of attempts.\n"


@pytest.fixture
def env(tmp_path):
    req = tmp_path / "requirements.json"
    req.write_text(json.dumps({"PAY-1": DOC}), encoding="utf-8")
    store = Store(req, tmp_path / "data")
    store.ensure()
    log = EventLog(store.events_path)
    runner = ReplayRunner(store, speed=10_000)
    disp = Dispatcher(log, store, runner, debounce_s=0.05)
    watcher = RequirementWatcher(store, log)
    store.save_snapshot({})   # baseline: PAY-1 counts as new on first scan
    yield store, log, disp, watcher
    disp.stop()
    watcher.stop()


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def types(log):
    evs, _ = log.read_from(0)
    return [e.type for e in evs]

import json

from conftest import DOC, types, wait_for


def test_watcher_emits_created_updated_removed(env):
    store, log, disp, watcher = env
    watcher.scan()
    assert "requirement.created" in types(log)
    store.write_requirements({"PAY-1": DOC + "\nx\n"}, via="tui")
    watcher.scan()
    evs, _ = log.read_from(0)
    updated = [e for e in evs if e.type == "requirement.updated"]
    assert updated and updated[-1].payload["via"] == "tui"
    store.write_requirements({}, via="cli")
    watcher.scan()
    assert "requirement.removed" in types(log)


def test_first_scan_without_snapshot_is_silent(env):
    store, log, disp, watcher = env
    store.snapshot_path.unlink()
    watcher.scan()
    assert types(log) == []
    assert store.load_snapshot() == {"PAY-1": __import__("reqreview.util", fromlist=["x"]).sha256_text(DOC)}


def test_external_change_triggers_review_without_click(env):
    store, log, disp, watcher = env
    disp.start()
    watcher.scan()
    assert wait_for(lambda: "review.completed" in types(log))
    ts = types(log)
    assert ts.index("requirement.created") < ts.index("review.queued") < ts.index("review.started") < ts.index("review.completed")
    assert store.status_of("PAY-1").key in ("ready", "attention")


def test_unchanged_content_is_skipped(env):
    store, log, disp, watcher = env
    disp.start()
    watcher.scan()
    assert wait_for(lambda: "review.completed" in types(log))
    # touch the file with identical content: snapshot hash is equal, so the watcher emits nothing at all
    store.write_requirements(store.load_requirements(), via="cli")
    watcher.scan()
    assert types(log).count("requirement.updated") == 0
    # but a synthetic event for the same content is skipped by the dispatcher (idempotency)
    log.append("requirement.updated", "PAY-1", "watcher", {})
    assert wait_for(lambda: "review.skipped" in types(log))
    evs, _ = log.read_from(0)
    assert [e.payload["reason"] for e in evs if e.type == "review.skipped"][-1] == "unchanged"


def test_approved_requirement_is_not_reopened(env):
    store, log, disp, watcher = env
    disp.start()
    watcher.scan()
    assert wait_for(lambda: "review.completed" in types(log))
    store.approve("PAY-1", 1)
    store.write_requirements({"PAY-1": DOC + "\nA new clause should apply.\n"}, via="tui")
    watcher.scan()
    assert wait_for(lambda: "review.skipped" in types(log))
    evs, _ = log.read_from(0)
    assert [e.payload["reason"] for e in evs if e.type == "review.skipped"][-1] == "approved"
    assert store.status_of("PAY-1").key == "stale"
    assert types(log).count("review.completed") == 1


def test_rapid_edits_are_debounced_into_one_job(env):
    store, log, disp, watcher = env
    disp.start()
    watcher.scan()
    assert wait_for(lambda: "review.completed" in types(log))
    disp.debounce_s = 0.6   # wide window so the three edits below land in one debounce
    for i in range(3):
        store.write_requirements({"PAY-1": DOC + f"\nEdit {i}.\n"}, via="tui")
        watcher.scan()
    assert wait_for(lambda: types(log).count("review.completed") == 2)
    assert types(log).count("review.started") == 2
    evs, _ = log.read_from(0)
    assert any(e.type == "review.skipped" and e.payload.get("reason") == "debounced" for e in evs)


def test_log_lines_are_valid_json(env):
    store, log, disp, watcher = env
    watcher.scan()
    for line in store.events_path.read_text(encoding="utf-8").splitlines():
        assert json.loads(line)["id"].startswith("evt_")

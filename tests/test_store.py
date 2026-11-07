from reqreview.agent import GradingPass, VerificationRun
from reqreview.util import sha256_text, utc_now

from conftest import DOC


def _run(status="satisfied"):
    return VerificationRun(requirement_doc=DOC, trace=[], analysis_markdown="x", instructions="i",
                           rubric="r", grading=[GradingPass(1, status, "", [], "x")], status=status)


def _save(store, n=1, status="satisfied", doc=DOC):
    store.save_run("PAY-1", _run(status), {"ticket_id": "PAY-1", "run_n": n, "req_hash": sha256_text(doc),
                                           "finished_at": utc_now(), "harness_version": "v1"})


def test_status_machine(env):
    store, *_ = env
    assert store.status_of("PAY-1").key == "new"
    _save(store)
    assert store.status_of("PAY-1").key == "ready"
    store.approve("PAY-1", 1)
    assert store.status_of("PAY-1").key == "approved"
    store.write_requirements({"PAY-1": DOC + "\nMore text.\n"}, via="tui")
    assert store.status_of("PAY-1").key == "stale"
    assert store.status_of("PAY-1", ("verifying", 2)).label == "Verifying (2)"


def test_needs_attention(env):
    store, *_ = env
    _save(store, status="needs_revision")
    assert store.status_of("PAY-1").key == "attention"


def test_harness_versions_are_immutable(env):
    store, *_ = env
    assert store.harness_versions() == ["v1"]
    v1 = store.harness_text("v1")
    v2 = store.add_version(v1 + "\nextra\n")
    assert v2 == "v2" and store.harness_text("v1") == v1
    store.activate("v2")
    assert store.active_harness() == "v2"


def test_atomic_write_marker(env):
    store, *_ = env
    store.write_requirements({"A-1": "x\n"}, via="cli")
    assert store.recent_write_via() == "cli"
    assert store.load_requirements() == {"A-1": "x\n"}

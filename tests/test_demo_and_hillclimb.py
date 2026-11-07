import json

import pytest

from reqreview.agent import DEFAULT_RUBRIC
from reqreview.demo_data import has_labelled_lines, synthesize_run
from reqreview.hillclimb import HarnessChange, analyse_demo, apply_change, build_summary, run_batch
from reqreview.runner import ReplayRunner, interleave

from conftest import DOC


def test_v1_harness_fails_first_pass_then_revises(env):
    store, *_ = env
    run = synthesize_run("PAY-1", DOC, store.harness_text("v1"), DEFAULT_RUBRIC)
    assert not has_labelled_lines(store.harness_text("v1"))
    assert len(run.grading) == 2 and run.grading[0].result == "needs_revision"
    assert {"clarity", "depth"} <= set(run.grading[0].failed)
    assert run.status == "satisfied" and "Resolved by:" in run.analysis_markdown
    kinds = [k for k, _ in interleave(run.trace, run.grading)]
    assert kinds.count("grade") == 2


def test_labelled_harness_passes_first_attempt(env):
    store, *_ = env
    text = store.harness_text("v1") + "\nResolved by: x\nEdge case: y\n"
    run = synthesize_run("PAY-1", DOC, text, DEFAULT_RUBRIC)
    assert len(run.grading) == 1 and run.grading[0].result == "satisfied"


def test_apply_change_inserts_under_section_and_rejects_non_prompt():
    md = "# A\n\n## Review guidance\n\n### Ambiguities\n\nold line\n\n### Facts\n\nfact line\n"
    ch = HarnessChange(target="prompt", issue="i", recurrence="r", evidence=[], section="### Ambiguities", change="NEW RULE")
    out = apply_change(ch, md)
    assert out.index("old line") < out.index("NEW RULE") < out.index("### Facts")
    with pytest.raises(ValueError):
        apply_change(ch.model_copy(update={"target": "model"}), md)


def test_full_hill_climb_story_in_demo_mode(env):
    store, *_ = env
    runner = ReplayRunner(store, speed=10_000)
    tickets = {"PAY-1": DOC, "ALERT-1": DOC.replace("Payment", "Alert"), "AUTH-1": DOC.replace("Payment", "Auth")}
    s1 = run_batch(store, runner, "v1", "v1", tickets)
    assert s1["first_attempt_pass_rate"]["clarity"] == 0.0
    change, _ = analyse_demo(store, "v1", speed=10_000)
    assert change.target == "prompt" and change.evidence
    v2 = store.add_version(apply_change(change, store.harness_text("v1")))
    s2 = run_batch(store, runner, v2, v2, tickets)
    assert s2["first_attempt_pass_rate"]["clarity"] == 1.0
    assert s2["first_attempt_satisfied"] == len(tickets)
    assert store.harness_text("v1") != store.harness_text(v2)


def test_summary_file_written(env):
    store, *_ = env
    run_batch(store, ReplayRunner(store, speed=10_000), "v1", "v1", {"PAY-1": DOC})
    assert json.loads((store.batch_dir("v1") / "summary.json").read_text())["n_runs"] == 1
    assert build_summary(store, "v1", "v1")["n_runs"] == 1

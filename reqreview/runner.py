"""Runners: the seam between the UI/dispatcher and the thing that actually reviews.

* `LiveRunner`   builds a fresh deepagents agent + workspace per job (D-07) and calls OpenRouter.
* `ReplayRunner` replays a recorded or synthetic run with realistic timing and no network (D-10).

Both implement the same `run(...)` signature, so the TUI has exactly one code path.
"""
from __future__ import annotations

import copy
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

from .agent import (
    DEFAULT_RUBRIC,
    VERIFICATION_MAX_TURNS,
    DictTicketProvider,
    GradingPass,
    ReviewTraceStep,
    VerificationRun,
    build_review_agent,
    load_run_with_meta,
    new_job_workspace,
    run_verification_loop_live,
)
from .util import sha256_text, stable_int, utc_now

StepCB = Callable[[ReviewTraceStep], None]
GradeCB = Callable[[GradingPass], None]
EventCB = Callable[[str, dict], None]


@dataclass
class Job:
    ticket_id: str
    document: str
    reason: str                 # manual | requirement.created | requirement.updated | batch
    correlation_id: str
    harness_version: str
    content_hash: str


@dataclass
class RunResult:
    run: VerificationRun
    model: str
    grader_model: str
    synthetic: bool = False


class Runner(Protocol):
    name: str
    model_name: str

    def run(self, job: Job, run_n: int, on_step: StepCB, on_grade: GradeCB, on_event: EventCB) -> RunResult: ...


def is_finish(step: ReviewTraceStep) -> bool:
    return step.actor == "Model reasoning" and (
        step.action == "Finishes the review" or step.action.startswith("Stops:"))


def interleave(trace: list[ReviewTraceStep], grading: list[GradingPass]) -> Iterator[tuple[str, Any]]:
    """Place each grading pass after the agent's k-th 'Finishes the review' step.

    The grader runs when the agent stops, so this reproduces the live order for stored runs.
    """
    gi = 0
    for s in trace:
        yield ("step", s)
        if is_finish(s) and gi < len(grading):
            yield ("grade", grading[gi])
            gi += 1
    while gi < len(grading):
        yield ("grade", grading[gi])
        gi += 1


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

class LiveRunner:
    name = "live"

    def __init__(self, store: Any, rubric: str = DEFAULT_RUBRIC, rubric_iterations: int = 3,
                 max_turns: int = VERIFICATION_MAX_TURNS):
        self.store = store
        self.rubric = rubric
        self.rubric_iterations = rubric_iterations
        self.max_turns = max_turns

    @property
    def model_name(self) -> str:
        from . import llm
        return llm.current_model("reviewer")

    @property
    def grader_model_name(self) -> str:
        from . import llm
        return llm.current_model("grader")

    def run(self, job: Job, run_n: int, on_step: StepCB, on_grade: GradeCB, on_event: EventCB) -> RunResult:
        """Run once; if a model is refused (403 / region / no endpoints), mark it bad and fail over."""
        from . import llm

        llm.set_event_sink(on_event)
        try:
            for attempt in range(3):
                used: list[str] = []
                try:
                    return self._run_once(job, on_step, on_grade, used)
                except Exception as exc:
                    if attempt < 2 and llm.is_access_error(exc):
                        llm.mark_bad(*used)
                        on_event("model_failover", {"note": f"{', '.join(used) or 'model'} refused "
                                                            f"({type(exc).__name__}); trying another model"})
                        continue
                    raise
            raise RuntimeError("unreachable")
        finally:
            llm.set_event_sink(None)

    def _run_once(self, job: Job, on_step: StepCB, on_grade: GradeCB, used: list[str]) -> RunResult:
        from . import llm

        workspace = new_job_workspace(self.store.workspace_root, self.store.harness_text(job.harness_version))
        try:
            reviewer = llm.get_chat_model("reviewer")
            grader = llm.get_chat_model("grader")
            used += [getattr(reviewer, "model_name", ""), getattr(grader, "model_name", "")]
            agent = build_review_agent(
                DictTicketProvider({job.ticket_id: job.document}),
                model=reviewer,
                grader_model=grader,
                max_turns=self.max_turns,
                workspace_dir=workspace,
                rubric_iterations=self.rubric_iterations,
                on_grade=on_grade,
            )
            run = run_verification_loop_live(agent, job.ticket_id, self.rubric, on_step=on_step)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        return RunResult(run=run, model=used[0], grader_model=used[1])


# ---------------------------------------------------------------------------
# Replay / demo
# ---------------------------------------------------------------------------

class ReplayRunner:
    """Replays a recorded run from `demo_dir/reviews/<ID>/run_*.json` if it matches the document,
    otherwise a synthetic run built from the document text (see demo_data.py)."""

    name = "demo replay"
    model_name = "demo-replay (scripted, no API calls)"
    grader_model_name = "demo-replay (scripted, no API calls)"

    def __init__(self, store: Any, demo_dir: Optional[Path] = None, speed: float = 1.0,
                 rubric: str = DEFAULT_RUBRIC):
        self.store = store
        self.demo_dir = Path(demo_dir) if demo_dir else None
        self.speed = max(speed, 0.01)
        self.rubric = rubric

    def _recorded(self, job: Job) -> Optional[VerificationRun]:
        if not self.demo_dir:
            return None
        d = self.demo_dir / "reviews" / job.ticket_id
        files = sorted(d.glob("run_*.json")) if d.exists() else []
        for p in reversed(files):
            try:
                run, _ = load_run_with_meta(p)
            except Exception:
                continue
            if isinstance(run, VerificationRun) and sha256_text(run.requirement_doc) == job.content_hash:
                return run
        return None

    def run(self, job: Job, run_n: int, on_step: StepCB, on_grade: GradeCB, on_event: EventCB) -> RunResult:
        from .demo_data import synthesize_run

        harness_text = self.store.harness_text(job.harness_version)
        recorded = self._recorded(job)
        run = copy.deepcopy(recorded) if recorded else synthesize_run(
            job.ticket_id, job.document, harness_text, self.rubric)
        run = replace(run, requirement_doc=job.document, instructions=harness_text)

        fast = job.reason == "batch"
        f = self.speed * (10 if fast else 1)

        def nap(seconds: float) -> None:
            time.sleep(seconds / f)

        if not fast and stable_int(job.ticket_id) % 3 == 0:
            nap(0.8)
            on_event("throttle", {"note": "simulated: client-side limiter holding a request (demo)"})
            nap(1.5)

        for kind, item in interleave(run.trace, run.grading):
            if kind == "step":
                nap(0.55 if item.actor == "Model reasoning" else 0.3)
                on_step(replace(item, ts=utc_now()))
            else:
                nap(1.2)
                on_grade(copy.deepcopy(item))
        return RunResult(run=run, model=self.model_name, grader_model=self.grader_model_name,
                         synthetic=recorded is None)

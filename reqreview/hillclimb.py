"""Hill-climb loop (ported from Lab6, adapted to the ticket-id + provider agent API).

    run_batch()  ->  summary.json (first-attempt pass rate per criterion)
    analyse()    ->  HarnessChange proposal (evidence quoted from the run files)
    apply_change() -> new AGENTS text (plain string work, no LLM)
    Human approves  -> Store.add_version() creates v(N+1); versions are never overwritten (D-09).

Live analysis uses a deepagents analyst that may only read the history and write /proposal.md.
Demo analysis derives the same structure from the (synthetic) run files, with no network.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .agent import criterion_names, DEFAULT_RUBRIC
from .runner import Job
from .util import atomic_write_text, new_id, sha256_text, utc_now

RUBRIC_ITERATIONS = 3


class HarnessChange(BaseModel):
    target: str = Field(description="What to change: 'prompt' (AGENTS.md) is the only auto-applicable target; "
                                    "anything else (tools, model, rubric) is reported read-only.")
    issue: str = Field(description="The recurring failure, in one or two sentences.")
    recurrence: str = Field(description="How often it happened, e.g. '6 of 6 runs fail clarity on the first attempt'.")
    evidence: list[str] = Field(description="Short verbatim quotes from the run files that prove the issue.")
    section: str = Field(description="Heading in AGENTS.md the change belongs under, e.g. '### Ambiguities'.")
    change: str = Field(description="The exact text to add to that section.")
    rejected: list[str] = Field(default_factory=list, description="Other fixes considered and why they were dropped.")


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def estimate_calls(n_docs: int, rubric_iterations: int = RUBRIC_ITERATIONS) -> int:
    """Rough model-call estimate: ~8 turns for the first attempt, ~6 per revision, +1 grader read each."""
    return n_docs * (8 + rubric_iterations * 6)


def run_batch(store: Any, runner: Any, label: str, harness_version: str, tickets: dict[str, str],
              on_progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """Run every ticket once with a given harness version. Resumable: finished documents are skipped."""
    d = store.batch_dir(label)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(d / "AGENTS.md", store.harness_text(harness_version))
    total = len(tickets)
    for i, (tid, doc) in enumerate(tickets.items(), start=1):
        out = d / f"run_{i:02d}_{tid}.json"
        if out.exists():
            if on_progress:
                on_progress(i, total, tid)
            continue
        job = Job(tid, doc, "batch", new_id("cor"), harness_version, sha256_text(doc))
        started = utc_now()
        result = runner.run(job, i, lambda s: None, lambda g: None, lambda k, p: None)
        payload = asdict(result.run)
        payload["meta"] = {"ticket_id": tid, "label": label, "harness_version": harness_version,
                           "model": result.model, "started_at": started, "finished_at": utc_now(),
                           "synthetic": result.synthetic}
        atomic_write_text(out, json.dumps(payload, indent=2, ensure_ascii=False))
        if on_progress:
            on_progress(i, total, tid)
    return build_summary(store, label, harness_version)


def _records(store: Any, label: str) -> list[dict]:
    out = []
    for p in sorted(store.batch_dir(label).glob("run_*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def build_summary(store: Any, label: str, harness_version: str) -> dict:
    recs = _records(store, label)
    totals: dict[str, list[int]] = {}
    for r in recs:
        g = r.get("grading") or []
        if not g:
            continue
        for c in g[0].get("criteria", []):
            t = totals.setdefault(c.get("name", "?"), [0, 0])
            t[1] += 1
            t[0] += 1 if c.get("passed") else 0
    summary = {
        "label": label,
        "harness_version": harness_version,
        "n_runs": len(recs),
        "first_attempt_pass_rate": {k: (v[0] / v[1] if v[1] else 0.0) for k, v in totals.items()},
        "first_attempt_satisfied": sum(1 for r in recs if (r.get("grading") or [{}])[0].get("result") == "satisfied"),
        "final_satisfied": sum(1 for r in recs if r.get("status") == "satisfied"),
        "runs": [{"ticket": r.get("meta", {}).get("ticket_id"), "passes": len(r.get("grading") or []),
                  "status": r.get("status")} for r in recs],
        "built_at": utc_now(),
    }
    atomic_write_text(store.batch_dir(label) / "summary.json", json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------

ANALYST_PROMPT = """\
You improve an AI agent's instructions using evidence from its past runs. You do NOT run the agent and you
never edit files other than /proposal.md.

The history for one batch is in /<label>/ :
- summary.json   first-attempt pass rate per rubric criterion
- AGENTS.md      the exact instructions the agent was given
- run_NN_<ticket>.json   one file per run: `grading[]` (per grader pass: `criteria[]` with `passed` and `gap`,
                 and `analysis_markdown`, the file as the grader saw it), `trace`, `status`, `rubric`

Procedure:
1. Read summary.json. Pick the criterion with the lowest first-attempt pass rate that repeats across runs.
2. Read AGENTS.md and at least three run files. Confirm the cause is in the INSTRUCTIONS (not luck).
3. Propose exactly ONE change. Quote evidence verbatim from the run files (gaps or analysis lines).
4. If the fix is not a prompt change (tools, model, rubric), set target to that and explain; do not invent a prompt fix.
5. Write a short human-readable /proposal.md, then return the structured HarnessChange.
Do not propose changes for one-off failures."""


def _save_proposal(store: Any, label: str, change: HarnessChange, md: str) -> None:
    store.history_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(store.history_dir / f"proposal_{label}.json", change.model_dump_json(indent=2))
    atomic_write_text(store.history_dir / f"proposal_{label}.md", md)


def load_proposal(store: Any, label: str) -> Optional[tuple[HarnessChange, str]]:
    try:
        change = HarnessChange.model_validate_json((store.history_dir / f"proposal_{label}.json").read_text("utf-8"))
        md = (store.history_dir / f"proposal_{label}.md").read_text("utf-8")
        return change, md
    except Exception:
        return None


def _proposal_md(change: HarnessChange) -> str:
    ev = "\n".join(f"- {e}" for e in change.evidence)
    return (f"# Proposed harness change\n\n**Target:** {change.target}\n\n**Issue:** {change.issue}\n\n"
            f"**Recurrence:** {change.recurrence}\n\n## Evidence\n{ev}\n\n**Section:** {change.section}\n\n"
            f"## Change\n{change.change}\n")


DEMO_CHANGE_TEXT = """\
Every bullet in this section MUST carry all three indented lines below, in this order. This overrides the
shorter shape shown in Procedure step 3, because the rubric checks these exact labels:

```
- **"the exact clause"**
  Reason: why the clause is under-specified
  Resolved by: the specific fact or decision that would settle it
  Edge case: a concrete production failure this ambiguity could cause
```

The labels `Resolved by:` and `Edge case:` must be written exactly like that. A line with a different label
does not count."""


def analyse_demo(store: Any, label: str, speed: float = 1.0) -> tuple[HarnessChange, str]:
    """Same structure as the live analyst, derived from the batch files (no network)."""
    time.sleep(2.5 / max(speed, 0.01))
    recs = _records(store, label)
    summary = store.load_summary(label) or {}
    rates: dict[str, float] = summary.get("first_attempt_pass_rate", {})
    if not recs or not rates:
        raise ValueError(f"no batch results for {label}; run the batch first")
    worst = min(rates, key=lambda k: rates[k])
    if rates[worst] >= 0.8:
        raise ValueError(f"no recurring issue: lowest first-attempt pass rate is {rates[worst]:.0%} ({worst})")
    failing = sorted(k for k, v in rates.items() if v < 0.8)
    gaps: list[str] = []
    n_fail = 0
    for r in recs:
        first = (r.get("grading") or [{}])[0]
        bad = [c for c in first.get("criteria", []) if not c.get("passed") and c.get("name") in failing]
        if bad:
            n_fail += 1
            gaps += [str(c.get("gap", "")) for c in bad if c.get("gap")]
    seen: list[str] = []
    for g in gaps:
        if g not in seen:
            seen.append(g)
    change = HarnessChange(
        target="prompt",
        issue=("The agent's Ambiguities bullets stop at a 'Reason:' line, but the rubric requires labelled "
               "'Resolved by:' and 'Edge case:' lines, so the first attempt fails and the grader has to send it back."),
        recurrence=f"{n_fail} of {len(recs)} runs fail {', '.join(failing)} on the first attempt",
        evidence=[f'"{g[:200]}"' for g in seen[:3]],
        section="### Ambiguities",
        change=DEMO_CHANGE_TEXT,
        rejected=["Raise the iteration budget: hides the problem and costs extra calls on every run.",
                  "Loosen the rubric: removes the check instead of teaching the agent."],
    )
    md = _proposal_md(change)
    _save_proposal(store, label, change, md)
    return change, md


def analyse_live(store: Any, label: str, model: Any = None) -> tuple[HarnessChange, str]:
    """Deepagents analyst: reads /<label>/ history, writes /proposal.md, returns a HarnessChange."""
    from deepagents import FilesystemMiddleware, FilesystemPermission, create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langchain.agents.middleware import ModelCallLimitMiddleware

    from . import llm

    root = store.history_dir
    if not store.load_summary(label):
        raise ValueError(f"no batch results for {label}; run the batch first")
    model = model or llm.get_chat_model("analyst")
    backend = FilesystemBackend(root_dir=str(root), virtual_mode=True)
    permissions = [
        FilesystemPermission(operations=["write"], paths=["/proposal.md"], mode="allow"),
        FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
    ]
    # Same wiring as Lab6; if your deepagents build takes `permissions=` on create_deep_agent instead,
    # switch these two lines (see MASTER_PLAN.md, section 13, item 5).
    fs = FilesystemMiddleware(backend=backend, tools=["ls", "read_file", "glob", "grep", "write_file"],
                              _permissions=permissions)
    graph = create_deep_agent(
        model=model,
        system_prompt=ANALYST_PROMPT.replace("<label>", label),
        backend=backend,
        middleware=[fs, ModelCallLimitMiddleware(run_limit=40, exit_behavior="end")],
        response_format=HarnessChange,
    )
    result = graph.invoke(
        {"messages": [{"role": "user", "content": f"The run history is in /{label}/. Read it and propose one "
                                                   "improvement to the agent's instructions."}]},
        config={"run_name": "Hill-climb analyst", "tags": ["loop-engineering", "hill-climb"]},
    )
    change = result.get("structured_response")
    if change is None:
        raise RuntimeError("the analyst returned no structured proposal")
    if not isinstance(change, HarnessChange):
        change = HarnessChange.model_validate(change)
    md = _proposal_md(change)
    _save_proposal(store, label, change, md)
    return change, md


def analyse(store: Any, label: str, live: bool, speed: float = 1.0) -> tuple[HarnessChange, str]:
    return analyse_live(store, label) if live else analyse_demo(store, label, speed)


# ---------------------------------------------------------------------------
# Apply (plain code; the agent never edits its own instructions)
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def apply_change(change: HarnessChange, agents_md: str) -> str:
    """Insert `change.change` at the end of `change.section` (or append a new section)."""
    if change.target.strip().lower() != "prompt":
        raise ValueError(f"target '{change.target}' is not a prompt change; it is shown read-only and not applied")
    lines = agents_md.splitlines()
    want = change.section.strip().lstrip("#").strip().lower()
    start = level = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and m.group(2).strip().lower() == want:
            start, level = i, len(m.group(1))
            break
    block = ["", *change.change.strip("\n").splitlines(), ""]
    if start is None:
        return agents_md.rstrip("\n") + "\n\n" + change.section.strip() + "\n" + "\n".join(block).strip("\n") + "\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = _HEADING.match(lines[j])
        if m and len(m.group(1)) <= level:
            end = j
            break
    new = lines[:end]
    while new and not new[-1].strip():
        new.pop()
    new += block + lines[end:]
    return "\n".join(new).rstrip("\n") + "\n"

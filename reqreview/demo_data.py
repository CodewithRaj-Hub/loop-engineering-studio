"""Synthetic review runs for demo mode (no API key, no network).

IMPORTANT (honesty): these runs are SCRIPTED, not model output. They are built from the requirement
text with templates so the full story is visible offline. Whether the first grading pass fails depends
on the harness text: if AGENTS.md lacks the labelled `Resolved by:` / `Edge case:` lines the rubric
requires, the first pass fails and the agent "revises"; once a harness version contains them, the
first pass is satisfied. That is the same cause-and-effect the live hill-climb loop discovers.

Recorded real runs placed in `demo/reviews/<ID>/run_*.json` take precedence (see ReplayRunner).
"""
from __future__ import annotations

import re

from .agent import (
    DEFAULT_RUBRIC,
    GradingPass,
    ReviewTraceStep,
    VerificationRun,
    _truncate,
    criterion_names,
)
from .util import stable_int, utc_now

VAGUE_WORDS = [
    "securely", "common", "reasonable", "appropriately", "timely", "appropriate", "efficiently", "easily",
    "immediate", "reliably", "sensibly", "certain", "special", "promptly", "manageable", "quickly",
    "aggressively", "entitled", "ownership", "grouped", "sensible", "customize",
]


def parse_requirement(document: str) -> tuple[str, list[str]]:
    lines = [ln.strip() for ln in document.splitlines() if ln.strip()]
    if lines and lines[0].upper().startswith("REQUIREMENT"):
        title = lines[0].split(":", 1)[-1].strip() or "Untitled"
        return title, lines[1:] or [title]
    return (lines[0] if lines else "Untitled"), (lines[1:] or lines)


def pick_vague(clauses: list[str]) -> list[tuple[str, str]]:
    """(clause, vague word) pairs; guaranteed non-empty so every list section has content."""
    out = []
    for c in clauses:
        low = c.lower()
        word = next((w for w in VAGUE_WORDS if w in low), None)
        if word:
            out.append((c, word))
    if not out:
        out = [(c, "the main behaviour") for c in clauses[:2]]
    return out


def has_labelled_lines(harness_text: str) -> bool:
    return "Resolved by:" in harness_text and "Edge case:" in harness_text


def build_analysis(title: str, clauses: list[str], vague: list[tuple[str, str]], labelled: bool,
                   drop_last: bool = False) -> str:
    shown = clauses[:-1] if (drop_last and len(clauses) > 1) else clauses
    shown_vague = [(c, w) for c, w in vague if c in shown] or vague[:1]
    lines = ["# Requirements Analysis", "", "## Confirmed Facts"]
    lines.append(f'- The requirement is titled "{title}".')
    for c in shown:
        lines.append(f"- The document states: {c}")
    lines += ["", "## Ambiguities"]
    for c, w in shown_vague:
        lines.append(f'- **"{c}"**')
        lines.append(f'  Reason: "{w}" is not defined or quantified, so two readers can build different behaviour.')
        if labelled:
            lines.append(f'  Resolved by: a measurable acceptance value for "{w}" agreed with the product owner '
                         "and written into the requirement.")
            lines.append(f'  Edge case: in production an undefined "{w}" lets a load spike or retry storm pass '
                         "review while still violating the stakeholder\u2019s intent.")
    lines += ["", "## Clarification Questions"]
    for _, w in shown_vague:
        lines.append(f'- What exact, testable value or rule defines "{w}"?')
    lines += ["", "## Assumptions"]
    for _, w in shown_vague:
        lines.append(f'- Until clarified, "{w}" is treated as a placeholder for a number to be confirmed.')
    n = len(shown_vague)
    lines += ["", "## Summary",
              f'"{title}" has {len(shown)} stated clauses; {n} of them need clarification before '
              "implementation. The open questions are listed above and should be settled with the "
              "product owner first."]
    return "\n".join(lines) + "\n"


def _step_pair(n: int, label: str, detail: str, sections: list[str], tool: str) -> tuple[list[ReviewTraceStep], int]:
    ts = utc_now()
    steps = [
        ReviewTraceStep(n + 1, "Model reasoning", f"Calls {label}", ts=ts),
        ReviewTraceStep(n + 2, "Tool", label, detail, sections=sections, ts=ts),
    ]
    return steps, n + 2


def synthesize_run(ticket_id: str, document: str, harness_text: str, rubric: str = DEFAULT_RUBRIC) -> VerificationRun:
    title, clauses = parse_requirement(document)
    vague = pick_vague(clauses)
    labelled = has_labelled_lines(harness_text)
    h = stable_int(ticket_id)
    comp_fail = (not labelled) and h % 5 == 0 and len(clauses) > 1

    first_md = build_analysis(title, clauses, vague, labelled=labelled, drop_last=comp_fail)
    final_md = build_analysis(title, clauses, vague, labelled=True)

    trace: list[ReviewTraceStep] = []
    n = 0
    ts = utc_now()
    trace += [ReviewTraceStep(1, "Model reasoning", "Calls fetch_requirement", ts=ts),
              ReviewTraceStep(2, "Tool", "fetch_requirement", _truncate(document), ts=ts)]
    n = 2
    wrote = "Updated file /analysis.md"
    for i, sec in enumerate(["Confirmed Facts", "Ambiguities", "Clarification Questions", "Assumptions", "Summary"]):
        tool = "write_file" if i == 0 else "edit_file"
        steps, n = _step_pair(n, f"{tool}: {sec}", wrote, [sec], tool)
        trace += steps
    n += 1
    trace.append(ReviewTraceStep(n, "Model reasoning", "Finishes the review", ts=utc_now()))

    names = criterion_names(rubric)
    last_clause = clauses[-1]
    first_clause = vague[0][0]

    def criteria(first_pass: bool) -> list[dict]:
        out = []
        for name in names:
            if first_pass and name == "completeness" and comp_fail:
                out.append({"name": name, "passed": False,
                            "gap": f'No line in /analysis.md addresses the clause: "{last_clause}".'})
            elif first_pass and name == "clarity" and not labelled:
                out.append({"name": name, "passed": False,
                            "gap": f'Ambiguities bullets have a "Reason:" line but no indented "Resolved by:" line, '
                                   f'e.g. the bullet for "{first_clause}".'})
            elif first_pass and name == "depth" and not labelled:
                out.append({"name": name, "passed": False,
                            "gap": f'No indented "Edge case:" line under the Ambiguities bullet for "{first_clause}".'})
            else:
                out.append({"name": name, "passed": True})
        return out

    c1 = criteria(True)
    ok1 = all(c["passed"] for c in c1)
    grading = [GradingPass(
        iteration=1,
        result="satisfied" if ok1 else "needs_revision",
        explanation=("All criteria verified against quoted lines in /analysis.md." if ok1 else
                     "read_file(/analysis.md) shows the failing criteria below; the agent must revise."),
        criteria=c1,
        analysis_markdown=first_md,
    )]

    if not ok1:
        redo = (["Confirmed Facts"] if comp_fail else []) + ["Ambiguities"]
        for sec in redo:
            steps, n = _step_pair(n, f"edit_file: {sec}", wrote, [sec], "edit_file")
            trace += steps
        n += 1
        trace.append(ReviewTraceStep(n, "Model reasoning", "Finishes the review", ts=utc_now()))
        grading.append(GradingPass(
            iteration=2, result="satisfied",
            explanation="Revised file now satisfies every criterion; quoted lines verified.",
            criteria=[{"name": nm, "passed": True} for nm in names],
            analysis_markdown=final_md,
        ))
        final = final_md
    else:
        final = first_md

    return VerificationRun(
        requirement_doc=document,
        trace=trace,
        analysis_markdown=final,
        instructions=harness_text,
        stage_order=[s for st in trace for s in st.sections],
        turns=sum(1 for s in trace if s.actor == "Model reasoning"),
        rubric=rubric,
        grading=grading,
        status="satisfied",
    )


_REASON_RE = re.compile(r"Reason:")


def looks_v1(markdown: str) -> bool:
    return bool(_REASON_RE.search(markdown)) and "Resolved by:" not in markdown

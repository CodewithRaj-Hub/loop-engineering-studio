"""EVENT > QUEUE > AGENT > GRADER > STORE > REPORT: lights up as a job moves through the loop."""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

STAGES = ["EVENT", "QUEUE", "AGENT", "GRADER", "STORE", "REPORT"]
_STYLE = {"idle": "dim", "active": "bold black on yellow", "done": "bold green", "failed": "bold white on red"}
_MARK = {"idle": "", "active": "\u25b6 ", "done": "\u2714 ", "failed": "\u2718 "}


class PipelineStrip(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._state = {s: "idle" for s in STAGES}
        self._info = "idle \u2014 waiting for events"

    def on_mount(self) -> None:
        self._redraw()

    def reset(self, info: str = "") -> None:
        self._state = {s: "idle" for s in STAGES}
        self._info = info or "idle \u2014 waiting for events"
        self._redraw()

    def set(self, stage: str, state: str, info: str | None = None) -> None:
        self._state[stage] = state
        if info is not None:
            self._info = info
        self._redraw()

    def _redraw(self) -> None:
        t = Text(" ")
        for i, s in enumerate(STAGES):
            st = self._state[s]
            t.append(f" {_MARK[st]}{s} ", style=_STYLE[st])
            if i < len(STAGES) - 1:
                t.append(" \u25b8 ", style="dim")
        t.append("    " + self._info, style="italic")
        self.update(t)

    def apply_event(self, ev) -> None:
        """Drive the strip from the event stream (agent/grader steps are set directly by the app)."""
        tid, typ, p = ev.subject, ev.type, ev.payload
        if typ.startswith("requirement.") and typ != "requirement.invalid":
            self.reset(f"{tid}: {typ} ({p.get('via', '?')})")
            self.set("EVENT", "done")
        elif typ == "review.queued":
            self.set("EVENT", "done")
            self.set("QUEUE", "active", f"{tid}: queued ({p.get('reason', '')})")
        elif typ == "review.started":
            self.set("EVENT", "done")
            self.set("QUEUE", "done")
            self.set("AGENT", "active", f"{tid}: run {p.get('run_n')} \u00b7 harness {p.get('harness_version')}")
        elif typ == "review.skipped":
            self.set("EVENT", "done", f"{tid}: skipped \u2014 {p.get('reason')}")
        elif typ == "review.completed":
            for s in ("QUEUE", "AGENT", "GRADER"):
                self._state[s] = "done"
            self.set("STORE", "done")
            self.set("REPORT", "done", f"{tid}: {p.get('status') or 'finished'} after {p.get('passes', '?')} grading pass(es)")
        elif typ == "review.failed":
            self.set("AGENT", "failed", f"{tid}: failed \u2014 {p.get('error', '')[:60]}")

"""Collapsible trace: Attempt n > model turn > tool call, and Grading pass k > criteria.

Items are ("step", ReviewTraceStep) | ("grade", GradingPass) | ("note", (text, style)).
The same widget renders a live run (items arrive one by one) and a stored run (`load_run`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text
from textual.widgets import Tree

from ...runner import interleave


def _parse(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


class TraceTree(Tree):
    def __init__(self, **kwargs: Any):
        super().__init__("No run selected", **kwargs)
        self.show_root = True
        self._reset_state()

    def _reset_state(self) -> None:
        self._attempt_no = 0
        self._attempt = None
        self._turn = None
        self._need_attempt = True
        self._t0 = None

    def _elapsed(self, ts: str) -> str:
        """'+12.4s' since the first step of this run (empty when timestamps are missing)."""
        t = _parse(ts)
        if t is None:
            return ""
        if self._t0 is None:
            self._t0 = t
        return f"+{(t - self._t0).total_seconds():.1f}s "

    # ---- lifecycle ----------------------------------------------------------
    def reset(self, label: Text | str) -> None:
        self.clear()
        self.root.set_label(label)
        self.root.expand()
        self._reset_state()

    def load_run(self, rv: Any) -> None:
        run = rv.run
        meta = rv.meta
        tag = " \u00b7 demo replay" if meta.get("synthetic") else ""
        self.reset(Text(f"{meta.get('ticket_id', '')} \u00b7 run {rv.n}{tag}", style="bold"))
        for item in interleave(run.trace, getattr(run, "grading", []) or []):
            self.add_item(item)
        self.finish(getattr(run, "status", ""))

    def finish(self, status: str) -> None:
        if not status:
            return
        ok = status == "satisfied"
        mark = "\u2714" if ok else "\u26a0"
        self.root.add_leaf(Text(f"{mark} Result: {status}", style="bold green" if ok else "bold red"))

    # ---- items ----------------------------------------------------------------
    def add_item(self, item: tuple[str, Any]) -> None:
        kind, obj = item
        if kind == "step":
            self._add_step(obj)
        elif kind == "grade":
            self._add_grade(obj)
        elif kind == "note":
            self._add_note(*obj)
        elif kind == "error":
            self._add_error(obj)
        self.scroll_end(animate=False)

    def _ensure_attempt(self) -> None:
        if self._attempt is not None and not self._need_attempt:
            return
        if self._attempt is not None:
            self._attempt.collapse()
        self._attempt_no += 1
        label = ("Attempt 1 \u2014 agent loop" if self._attempt_no == 1
                 else f"Attempt {self._attempt_no} \u2014 revision after grader feedback")
        self._attempt = self.root.add(Text(label, style="bold cyan"), expand=True)
        self._need_attempt = False
        self._turn = None

    def _add_step(self, s: Any) -> None:
        self._ensure_attempt()
        if s.actor == "Model reasoning":
            if self._turn is not None:
                self._turn.collapse()
            finishing = s.action == "Finishes the review" or s.action.startswith("Stops:")
            self._turn = self._attempt.add(
                Text.assemble((self._elapsed(s.ts), "dim"), ("\U0001f9e0 ", ""),
                              (s.action, "bold green" if finishing else "bold")), expand=True)
        else:
            parent = self._turn if self._turn is not None else self._attempt
            err = s.detail.lower().startswith("error")
            label = Text.assemble((self._elapsed(s.ts), "dim"), ("\U0001f527 ", ""),
                                  (s.action, "bold red" if err else "yellow"))
            if s.detail:
                label.append(f"  \u2192 {s.detail[:90]}", style="dim")
            parent.add_leaf(label)

    def _add_grade(self, g: Any) -> None:
        ok = g.result == "satisfied"
        node = self.root.add(
            Text.assemble(("\u2696 ", ""), (f"Grading pass {g.iteration}: {g.result}", "bold green" if ok else "bold red")),
            expand=True)
        for c in g.criteria:
            passed = bool(c.get("passed"))
            line = Text.assemble(("\u2714 " if passed else "\u2718 ", "green" if passed else "red"),
                                 (str(c.get("name", "?")), "bold"))
            gap = c.get("gap")
            if gap and not passed:
                line.append(f" \u2014 {str(gap)[:140]}", style="dim red")
            node.add_leaf(line)
        if self._attempt is not None:
            self._attempt.collapse()
        self._need_attempt = not ok

    def _add_error(self, info: dict) -> None:
        """A failed run: what happened, why, and how to fix it, right in the trace."""
        node = self.root.add(Text.assemble(("\u2716 ", "bold red"), (info.get("title") or "The review failed", "bold red")),
                             expand=True)
        for label, key, style in (("Likely cause", "cause", "white"), ("How to fix", "fix", "bold green"),
                                  ("Raw error", "error", "dim")):
            if info.get(key):
                node.add_leaf(Text.assemble((f"{label}: ", "bold"), (str(info[key])[:400], style)))
        self.scroll_end(animate=False)

    def _add_note(self, text: str, style: str = "dark_orange") -> None:
        parent = self._turn if self._turn is not None else (self._attempt or self.root)
        parent.add_leaf(Text(f"\u23f3 {text}", style=style))

"""Event log table with correlation highlighting: select an event to light up its whole chain."""
from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable

from ...util import clock

_COLOURS = [
    ("requirement.", "cyan"), ("review.queued", "dodger_blue1"), ("review.skipped", "grey62"),
    ("review.started", "yellow"), ("grade.failed", "red"), ("grade.passed", "green"),
    ("review.completed", "bright_green"), ("review.approved", "bright_green"), ("review.failed", "red"),
    ("llm.", "dark_orange"), ("harness.", "magenta"),
]


def colour_for(event_type: str) -> str:
    for prefix, colour in _COLOURS:
        if event_type.startswith(prefix):
            return colour
    return "white"


def _detail(ev) -> str:
    p = ev.payload
    keys = ("title", "reason", "via", "status", "iteration", "failed_criteria", "run_n", "harness_version", "kind", "note",
            "error", "label", "version", "passes")
    return " ".join(f"{k}={p[k]}" for k in keys if k in p and p[k] not in ("", None, []))[:70]


class EventLog(DataTable):
    MAX_ROWS = 400

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._events: dict[str, object] = {}
        self._order: list[str] = []
        self.add_column("Time", key="time", width=8)
        self.add_column("Type", key="type", width=18)
        self.add_column("Subject", key="subject", width=11)
        self.add_column("Source", key="source", width=10)
        self.add_column("Detail", key="detail")

    def _type_cell(self, ev, hot: bool = False) -> Text:
        style = f"bold {colour_for(ev.type)}" + (" reverse" if hot else "")
        return Text(ev.type, style=style)

    def add_event(self, ev) -> None:
        self._events[ev.id] = ev
        self._order.append(ev.id)
        self.add_row(clock(ev.ts), self._type_cell(ev), ev.subject, ev.source, _detail(ev), key=ev.id)
        if len(self._order) > self.MAX_ROWS:
            old = self._order.pop(0)
            self._events.pop(old, None)
            try:
                self.remove_row(old)
            except Exception:
                pass
        self.scroll_end(animate=False)

    def highlight_chain(self, event_id: str) -> None:
        ev = self._events.get(event_id)
        if ev is None:
            return
        cid = ev.correlation_id
        for eid in self._order:
            other = self._events[eid]
            try:
                self.update_cell(eid, "type", self._type_cell(other, hot=(other.correlation_id == cid)))
            except Exception:
                pass

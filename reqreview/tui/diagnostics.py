"""Tab 4: every problem in one place, in plain English: what happened, likely cause, how to fix, raw error."""
from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Label, Static

from .. import llm
from ..diag import describe_retry, explain
from ..util import clock

_LEVEL = {"ERROR": "bold white on red", "WARN": "bold black on dark_orange", "INFO": "bold white on grey37"}


class DiagnosticsPane(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._rows: dict[str, dict] = {}

    @property
    def ctx(self):
        return self.app.ctx

    def compose(self) -> ComposeResult:
        yield Static("", id="diag-status")
        with Horizontal(id="diag-row"):
            with Vertical(id="diag-left"):
                yield Label("Problems and warnings (select one)", classes="title")
                yield DataTable(id="diag-table", cursor_type="row")
                with Horizontal(classes="btnrow"):
                    yield Button("Re-check provider", id="btn-recheck", variant="primary")
                    yield Button("Clear", id="btn-clear")
            with Vertical(id="diag-right"):
                yield Label("What happened and how to fix it", classes="title")
                with VerticalScroll():
                    yield Static("Nothing to report yet. Failures, warnings and throttling show up here, "
                                 "explained in plain English. Full logs: data/logs/studio.log and errors.log "
                                 "(or run `reqreview logs`).", id="diag-detail")

    def on_mount(self) -> None:
        t = self.query_one("#diag-table", DataTable)
        t.add_column("Time", key="t", width=8)
        t.add_column("Level", key="l", width=7)
        t.add_column("Subject", key="s", width=11)
        t.add_column("What happened", key="w")
        self.refresh_status()

    def refresh_status(self) -> None:
        if self.ctx.demo:
            text = "Demo mode: scripted replay, no provider is called. Run without --demo to use a real model."
        else:
            text = (f"Provider: {llm.provider()} \u00b7 key {llm.key_env_name()}: "
                    f"{'found' if llm.has_key() else 'MISSING'} \u00b7 reviewer model: {self.ctx.runner.model_name}")
        self.query_one("#diag-status", Static).update(Text(text, style="bold"))

    def add_problem(self, ev) -> None:
        """Called by the app for review.failed, llm.retry and requirement.invalid events."""
        p = ev.payload
        if ev.type == "review.failed":
            level, title = "ERROR", p.get("title") or "The review failed"
            cause, fix, raw = p.get("cause", ""), p.get("fix", ""), p.get("error", "")
        elif ev.type == "llm.retry":
            kind, note = p.get("kind", "error"), p.get("note", "")
            level, title = describe_retry(kind, note)
            exp = explain(note) if note else None
            cause = exp.cause if exp and exp.title != "Unexpected error" else "See the raw message."
            fix = exp.fix if exp and exp.title != "Unexpected error" else "Usually transient; no action needed unless it repeats."
            raw = note
        else:  # requirement.invalid
            level, title = "ERROR", "requirements.json is not valid"
            exp = explain("jsondecodeerror")
            cause, fix, raw = exp.cause, exp.fix, p.get("error", "")
        self._rows[ev.id] = {"level": level, "title": title, "cause": cause, "fix": fix, "raw": raw,
                             "subject": ev.subject, "ts": ev.ts, "cid": ev.correlation_id}
        self.query_one("#diag-table", DataTable).add_row(clock(ev.ts), Text(f" {level} ", style=_LEVEL[level]),
                                                         ev.subject, title[:90], key=ev.id)

    @on(DataTable.RowHighlighted, "#diag-table")
    def _show(self, event: DataTable.RowHighlighted) -> None:
        row = self._rows.get(event.row_key.value) if event.row_key is not None else None
        if not row:
            return
        t = Text()
        t.append(f"{row['title']}\n\n", style="bold red" if row["level"] == "ERROR" else "bold dark_orange")
        t.append("Likely cause\n", style="bold cyan")
        t.append(f"{row['cause'] or 'Unknown'}\n\n")
        t.append("How to fix\n", style="bold green")
        t.append(f"{row['fix'] or 'No action needed.'}\n\n")
        t.append("Raw error (copy this when asking for help)\n", style="bold")
        t.append(f"{row['raw'] or '-'}\n\n", style="dim")
        t.append(f"Ticket {row['subject']} \u00b7 chain {row['cid']} \u00b7 more detail: data/logs/errors.log", style="italic dim")
        self.query_one("#diag-detail", Static).update(t)

    @on(Button.Pressed, "#btn-clear")
    def _clear(self) -> None:
        self._rows.clear()
        self.query_one("#diag-table", DataTable).clear()

    @on(Button.Pressed, "#btn-recheck")
    def _recheck(self) -> None:
        if self.ctx.demo:
            self.app.notify("Demo mode makes no provider calls.", severity="warning")
            return
        self.query_one("#diag-detail", Static).update("Probing tool calling on the candidate models\u2026")
        self._probe_worker()

    @work(thread=True, exclusive=True, group="probe")
    def _probe_worker(self) -> None:
        t = Text()
        t.append(f"Provider {llm.provider()} \u00b7 one request per candidate\n\n", style="bold")
        for m in llm.pick_models("reviewer")[:5]:
            res = llm.probe(m)
            t.append(("OK    " if res["ok"] else "FAIL  "), style="bold green" if res["ok"] else "bold red")
            t.append(f"{m}  {res['seconds']}s\n")
            if not res["ok"]:
                exp = explain(res["detail"])
                t.append(f"      {exp.title}\n      Fix: {exp.fix}\n      Raw: {res['detail']}\n", style="dim")
        self.app.call_from_thread(self.query_one("#diag-detail", Static).update, t)

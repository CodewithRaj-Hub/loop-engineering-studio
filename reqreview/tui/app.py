"""Loop Engineering Studio: the Textual app.

Threading rule: the dispatcher and agents run in worker threads. Anything that touches a widget goes
through `call_from_thread` (hooks) or happens in the UI thread (the 0.25 s event-log poll).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Markdown, Static, TabbedContent, TabPane

from ..agent import section_counts
from ..diag import describe_retry
from ..events import Event, Hooks
from ..sample_data import SIM_POOL
from ..util import clock, new_id, utc_now
from .diagnostics import DiagnosticsPane
from .improve import ImprovePane
from .screens import ConfirmScreen, HelpScreen, RequirementScreen, UploadScreen
from .widgets.diff_view import DiffView
from .widgets.event_log import EventLog
from .widgets.pipeline_strip import PipelineStrip
from .widgets.requirements_table import RequirementsTable
from .widgets.trace_tree import TraceTree


class StudioApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "Loop Engineering Studio"
    BINDINGS = [
        Binding("1", "tab('studio')", "Studio"),
        Binding("2", "tab('events')", "Event loop"),
        Binding("3", "tab('improve')", "Improve"),
        Binding("4", "tab('diag')", "Diagnostics"),
        Binding("u", "upload", "Upload"),
        Binding("r", "review", "Review"),
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("p", "approve", "Approve"),
        Binding("s", "simulate", "Sim webhook"),
        Binding("i", "analyse", "Analyse"),
        Binding("t", "theme", "Theme"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, ctx: Any, autoplay: bool = False):
        super().__init__()
        self.ctx = ctx
        self.autoplay = autoplay
        self.selected_tid: Optional[str] = None
        self._offset = 0
        self._dirty = True
        self._start_ts = utc_now()
        self._buf: dict[str, dict] = {}          # live runs: tid -> {"run_n": int, "items": [...]}
        self._invalid_warned = False
        self._pending_select: Optional[str] = None
        self.quota_text = "n/a"
        self._model_text = "demo" if ctx.demo else "live"

    # ---- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PipelineStrip(id="pipeline")
        with TabbedContent(initial="studio", id="tabs"):
            with TabPane("\u2460 Review Studio", id="studio"):
                with Horizontal(id="studio-row"):
                    with Vertical(id="left"):
                        yield Label("Requirements", classes="title")
                        yield RequirementsTable(id="req-table")
                        with Horizontal(classes="btnrow"):
                            yield Button("Upload", id="btn-upload")
                            yield Button("Review", id="btn-review", variant="primary")
                            yield Button("Approve", id="btn-approve", variant="success")
                        with Horizontal(classes="btnrow"):
                            yield Button("Add", id="btn-add")
                            yield Button("Edit", id="btn-edit")
                            yield Button("Sim webhook", id="btn-sim")
                    with Vertical(id="center"):
                        yield Label("Trace: agent loop \u00b7 verification loop", classes="title")
                        yield TraceTree(id="trace")
                    with Vertical(id="right"):
                        with TabbedContent(initial="rep", id="detail-tabs"):
                            with TabPane("Report", id="rep"):
                                yield Static("", id="rep-badges")
                                with VerticalScroll():
                                    yield Markdown("", id="rep-md")
                            with TabPane("Grading", id="grd"):
                                with VerticalScroll():
                                    yield Static("", id="grd-static")
                            with TabPane("Requirement", id="reqtab"):
                                with VerticalScroll():
                                    yield Static("", id="req-static")
                            with TabPane("Diff", id="dif"):
                                with VerticalScroll():
                                    yield DiffView(id="dif-view")
                            with TabPane("Runs", id="runs"):
                                yield DataTable(id="runs-table", cursor_type="row")
            with TabPane("\u2461 Event Loop", id="events"):
                with Horizontal(id="events-row"):
                    with Vertical(id="ev-left"):
                        yield Label("Event log (select a row to highlight its chain)", classes="title")
                        yield EventLog(id="event-table")
                    with Vertical(id="ev-right"):
                        yield Label("Queue", classes="title")
                        yield Static("", id="queue-panel")
                        yield Label("Ways to trigger the loop", classes="title")
                        yield Static("", id="producers")
            with TabPane("\u2462 Improve (Hill Climb)", id="improve"):
                yield ImprovePane(id="improve-pane")
            with TabPane("\u2463 Diagnostics", id="diag"):
                yield DiagnosticsPane(id="diag-pane")
        yield Footer()

    # ---- startup / shutdown -----------------------------------------------------
    def on_mount(self) -> None:
        self.ctx.dispatcher.hooks = Hooks(on_step=self._h_step, on_grade=self._h_grade, on_state=self._h_state)
        self.ctx.start()
        t = self.query_one("#runs-table", DataTable)
        for key, label in (("n", "Run"), ("s", "Status"), ("p", "Passes"), ("t", "Turns"), ("h", "Harness"), ("w", "When")):
            t.add_column(label, key=key)
        self.query_one("#producers", Static).update(self._producers_text())
        self.refresh_subtitle()
        self._reload_table()
        self.set_interval(0.25, self._poll)
        self.set_interval(0.5, self._tick_queue)
        if not self.ctx.demo:
            self._quota_worker()
            self.set_interval(120, self._quota_worker)
        if self.autoplay:
            self.set_timer(6.0, self.action_simulate)

    def on_unmount(self) -> None:
        self.ctx.stop()

    def refresh_subtitle(self) -> None:
        mode = "DEMO \u00b7 scripted replay, no API calls" if self.ctx.demo else f"{self._model_text} \u00b7 {self.quota_text}"
        self.sub_title = f"{mode} \u00b7 harness {self.ctx.store.active_harness()}"

    @work(thread=True, exclusive=True, group="quota")
    def _quota_worker(self) -> None:
        from .. import llm
        try:
            model = self.ctx.runner.model_name
            quota = llm.quota_text()
        except Exception:
            return
        self.call_from_thread(self._set_quota, model, quota)

    def _set_quota(self, model: str, quota: str) -> None:
        self._model_text, self.quota_text = model, quota
        self.refresh_subtitle()

    def _producers_text(self) -> Text:
        req = self.ctx.store.requirements_path
        demo_flag = " --demo" if self.ctx.demo else ""
        t = Text()
        t.append("The loop reacts to changes in the system of record:\n", style="bold")
        t.append(f"{req}\n\n", style="cyan")
        t.append("1  ", style="bold yellow")
        t.append("Add / Edit buttons here (they write that file)\n")
        t.append("2  ", style="bold yellow")
        t.append("Edit the file in any editor and save\n")
        t.append("3  ", style="bold yellow")
        t.append("In another terminal (fake Jira):\n")
        t.append(f'   reqreview sim add NEW-1{demo_flag} --text "REQUIREMENT: Demo\\n\\nIt should be fast."\n', style="green")
        t.append("4  ", style="bold yellow")
        t.append("Press s here to simulate that webhook\n\n")
        t.append("Rules: debounce 1.5 s \u00b7 review only if the content hash changed \u00b7 Approved work is not "
                 "re-opened by an edit (it becomes Stale) \u00b7 one review at a time.", style="dim")
        return t

    # ---- dispatcher hooks (worker threads) -----------------------------------------
    def _h_step(self, job, step) -> None:
        self._marshal(self._on_step, job.ticket_id, step)

    def _h_grade(self, job, gp) -> None:
        self._marshal(self._on_grade, job.ticket_id, gp)

    def _h_state(self, _tid: str) -> None:
        self._marshal(self._mark_dirty)

    def _marshal(self, fn, *args) -> None:
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            pass

    def _mark_dirty(self) -> None:
        self._dirty = True

    def _live_buf(self, tid: str) -> dict:
        buf = self._buf.get(tid)
        if buf is None or buf.get("failed"):
            buf = {"run_n": self.ctx.store.next_run_n(tid), "items": []}
            self._buf[tid] = buf
            if tid == self.selected_tid:
                self.query_one(TraceTree).reset(Text(f"{tid} \u00b7 run {buf['run_n']} \u00b7 live", style="bold yellow"))
        return buf

    def _on_step(self, tid: str, step) -> None:
        buf = self._live_buf(tid)
        item = ("step", step)
        buf["items"].append(item)
        if tid == self.selected_tid:
            self.query_one(TraceTree).add_item(item)
        if step.actor == "Model reasoning" and (step.action == "Finishes the review" or step.action.startswith("Stops:")):
            strip = self.query_one(PipelineStrip)
            strip.set("AGENT", "done")
            strip.set("GRADER", "active", f"{tid}: grader is reading /analysis.md")

    def _on_grade(self, tid: str, gp) -> None:
        buf = self._live_buf(tid)
        item = ("grade", gp)
        buf["items"].append(item)
        if tid == self.selected_tid:
            self.query_one(TraceTree).add_item(item)
        strip = self.query_one(PipelineStrip)
        if gp.result == "satisfied":
            strip.set("GRADER", "done", f"{tid}: grader satisfied")
        else:
            strip.set("GRADER", "failed", f"{tid}: grader failed {', '.join(gp.failed)} \u2192 revising")
            strip.set("AGENT", "active")

    # ---- event polling (UI thread) ----------------------------------------------------
    def _poll(self) -> None:
        events, self._offset = self.ctx.log.read_from(self._offset)
        if events:
            for ev in events:
                self._handle_event(ev)
        if self._dirty:
            self._dirty = False
            self._reload_table()

    def _handle_event(self, ev: Event) -> None:
        log = self.query_one(EventLog)
        log.add_event(ev)
        fresh = ev.ts >= self._start_ts
        if fresh:
            self.query_one(PipelineStrip).apply_event(ev)
        t = ev.type
        if t.startswith(("requirement.", "review.", "grade.")):
            self._dirty = True
        if t == "requirement.invalid" and fresh and not self._invalid_warned:
            self._invalid_warned = True
            self.notify("requirements.json is not valid JSON {id: text}. Fix it and save again.", severity="error")
        if t == "review.started" and fresh:
            self._live_buf(ev.subject)
        if t == "llm.retry" and fresh:
            note = describe_retry(ev.payload.get("kind", "error"), ev.payload.get("note", ""))[1]
            buf = self._buf.get(ev.subject)
            if buf is not None:
                item = ("note", (note, "dark_orange"))
                buf["items"].append(item)
                if ev.subject == self.selected_tid:
                    self.query_one(TraceTree).add_item(item)
        if t in ("review.failed", "requirement.invalid") or (t == "llm.retry" and fresh):
            self.query_one(DiagnosticsPane).add_problem(ev)
        if t == "review.failed":
            buf = self._live_buf(ev.subject)
            buf["failed"] = True
            item = ("error", ev.payload)
            buf["items"].append(item)
            if ev.subject == self.selected_tid:
                self.query_one(TraceTree).add_item(item)
                self._fill_details(ev.subject, self.ctx.store.latest(ev.subject))
            if fresh:
                self.notify(f"{ev.payload.get('title', 'Review failed')}\nFix: {ev.payload.get('fix', '')[:160]}\n"
                            "Press 4 for details.", title=f"{ev.subject} failed", severity="error", timeout=12)
        if t == "review.completed":
            self._buf.pop(ev.subject, None)
            if ev.subject == self.selected_tid:
                self._show(ev.subject)
            if fresh:
                self.notify(f"{ev.subject}: {ev.payload.get('status') or 'finished'}", title="Review stored")
        if t.startswith("harness."):
            self.query_one(ImprovePane).refresh_all()
            self.refresh_subtitle()

    def _tick_queue(self) -> None:
        snap = self.ctx.dispatcher.snapshot()
        t = Text()
        run = snap["running"]
        t.append("Running   ", style="bold")
        t.append(f"{run.ticket_id} ({run.reason})\n" if run else "idle\n", style="yellow" if run else "dim")
        t.append("Waiting   ", style="bold")
        t.append((", ".join(snap["queued"]) or "\u2014") + "\n")
        t.append("Debounce  ", style="bold")
        t.append(", ".join(f"{tid} {left:.1f}s" for tid, left in snap["pending"]) or "\u2014")
        if snap["paused"]:
            t.append("\n\nQueue paused while a hill-climb batch runs.", style="dark_orange")
        self.query_one("#queue-panel", Static).update(t)

    @on(DataTable.RowHighlighted, "#event-table")
    def _event_highlight(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value:
            self.query_one(EventLog).highlight_chain(event.row_key.value)

    # ---- requirements table / details ----------------------------------------------------
    def _reload_table(self) -> None:
        store, disp = self.ctx.store, self.ctx.dispatcher
        try:
            reqs = store.load_requirements()
        except Exception:
            reqs = {}
        rows = []
        for tid, doc in reqs.items():
            latest = store.latest(tid)
            rows.append((tid, store.title_of(doc), store.status_of(tid, disp.live_of(tid)),
                         clock(latest.meta.get("finished_at", "")) if latest else "",
                         latest.meta.get("harness_version", "") if latest else ""))
        table = self.query_one(RequirementsTable)
        table.reload(rows)
        if self._pending_select and self._pending_select in reqs:
            tid = self._pending_select
            self._pending_select = None
            table.move_cursor(row=table.get_row_index(tid))
            self._select(tid)
        elif reqs and (self.selected_tid not in reqs):
            self._select(next(iter(reqs)))
        elif self.selected_tid in reqs:
            self._refresh_details_only()

    @on(DataTable.RowHighlighted, "#req-table")
    def _req_highlight(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is not None and event.row_key.value and event.row_key.value != self.selected_tid:
            self._select(event.row_key.value)

    def _select(self, tid: str) -> None:
        self.selected_tid = tid
        self._show(tid)

    def _refresh_details_only(self) -> None:
        """Status changed for the selected ticket: refresh report/runs without rebuilding a live tree."""
        if self.selected_tid and self.selected_tid not in self._buf:
            rv = self.ctx.store.latest(self.selected_tid)
            self._fill_details(self.selected_tid, rv)

    def _show(self, tid: str) -> None:
        store = self.ctx.store
        tree = self.query_one(TraceTree)
        doc = store.load_requirements_safe().get(tid, "")
        self.query_one("#req-static", Static).update(Text(doc.strip()))
        live = self._buf.get(tid)
        rv = store.latest(tid)
        if live is not None:
            tree.reset(Text(f"{tid} \u00b7 run {live['run_n']} \u00b7 live", style="bold yellow"))
            for item in live["items"]:
                tree.add_item(item)
        elif rv is not None:
            tree.load_run(rv)
        else:
            tree.reset(Text(f"{tid} \u00b7 not reviewed yet", style="dim"))
        self._fill_details(tid, rv)

    def _fill_details(self, tid: str, rv) -> None:
        store = self.ctx.store
        md = self.query_one("#rep-md", Markdown)
        badges = self.query_one("#rep-badges", Static)
        live = tid in self._buf and not self._buf[tid].get("failed")
        err = self._error_markdown(tid)
        if rv is None:
            md.update(err or ("*Review in progress\u2026 the report appears when the grader is satisfied or the budget ends.*"
                      if live else "*No review yet.* Press **Review** (`r`), or change the requirement and let the event loop pick it up."))
            badges.update("")
            self.query_one("#grd-static", Static).update("")
            self.query_one(DiffView).show_message("")
            self.query_one("#runs-table", DataTable).clear()
            return
        run = rv.run
        status = store.status_of(tid, self.ctx.dispatcher.live_of(tid))
        counts = section_counts(run.analysis_markdown)
        b = Text()
        b.append(f" {status.label} ", style=f"bold {status.fg} on {status.bg}")
        b.append(f"  run {rv.n} \u00b7 harness {rv.meta.get('harness_version', '?')} \u00b7 {rv.passes} grading pass(es)", style="dim")
        if rv.meta.get("synthetic"):
            b.append(" \u00b7 [demo replay]", style="italic dark_orange")
        b.append("\n")
        b.append(" \u00b7 ".join(f"{k} {v}" for k, v in counts.items()), style="cyan")
        badges.update(b)
        md.update(err + run.analysis_markdown)
        # grading table
        grading = getattr(run, "grading", []) or []
        table = Table(expand=True, show_lines=True)
        table.add_column("Pass", width=5)
        table.add_column("Result", width=15)
        table.add_column("Criteria")
        for g in grading:
            lines = []
            for c in g.criteria:
                ok = c.get("passed")
                mark = "\u2714" if ok else "\u2718"
                gap = f" \u2014 {c.get('gap')}" if (c.get("gap") and not ok) else ""
                lines.append(f"{mark} {c.get('name')}{gap}")
            table.add_row(str(g.iteration), g.result, "\n".join(lines))
        self.query_one("#grd-static", Static).update(table if grading else Text("No grading passes recorded.", style="dim"))
        # diff: first attempt vs final
        dv = self.query_one(DiffView)
        if len(grading) >= 2:
            dv.show(grading[0].analysis_markdown, run.analysis_markdown, "first attempt", "final")
        else:
            dv.show_message("Satisfied on the first attempt (or no grading): nothing to compare.")
        # runs
        rt = self.query_one("#runs-table", DataTable)
        rt.clear()
        for r in reversed(store.load_runs(tid)):
            rt.add_row(str(r.n), r.status or "?", str(r.passes), str(r.run.turns), r.meta.get("harness_version", ""),
                       clock(r.meta.get("finished_at", "")))

    def _error_markdown(self, tid: str) -> str:
        """Plain-English failure block for a ticket whose latest attempt crashed, else ''."""
        store = self.ctx.store
        e = store.load_error(tid)
        if not e or e.get("req_hash") != store.current_hash(tid):
            return ""
        latest = store.latest(tid)
        if latest and e.get("ts", "") <= latest.meta.get("finished_at", ""):
            return ""
        return (f"## \u2716 {e.get('title', 'The last review failed')}\n\n"
                f"**Likely cause:** {e.get('cause', 'unknown')}\n\n**How to fix:** {e.get('fix', 'see data/logs/errors.log')}\n\n"
                f"`{str(e.get('error', ''))[:300]}`\n\n---\n\n")

    # ---- actions ------------------------------------------------------------------------------
    def _modal_open(self) -> bool:
        return isinstance(self.screen, ModalScreen)

    def action_tab(self, name: str) -> None:
        if not self._modal_open():
            self.query_one("#tabs", TabbedContent).active = name

    def action_review(self) -> None:
        if self._modal_open():
            return
        tid = self.selected_tid
        if not tid:
            self.notify("Select a requirement first.", severity="warning")
            return
        if not self.ctx.dispatcher.submit_manual(tid):
            self.notify(f"{tid} is already queued or running.", severity="warning")

    def action_approve(self) -> None:
        if self._modal_open():
            return
        tid, store = self.selected_tid, self.ctx.store
        if not tid:
            return
        rv = store.latest(tid)
        if rv is None:
            self.notify("Nothing to approve yet: review it first.", severity="warning")
        elif rv.status != "satisfied":
            self.notify("The grader did not satisfy this review; approval is disabled.", severity="warning")
        elif rv.meta.get("req_hash") != store.current_hash(tid):
            self.notify("The requirement changed after this review (Stale). Review it again first.", severity="warning")
        elif store.is_approved_latest(tid):
            self.notify(f"{tid} is already approved.")
        else:
            store.approve(tid, rv.n)
            self.ctx.log.append("review.approved", tid, "human", {"run_n": rv.n}, new_id("cor"))
            self._dirty = True

    def action_add(self) -> None:
        if not self._modal_open():
            self.push_screen(RequirementScreen("add"), self._after_requirement)

    def action_edit(self) -> None:
        if self._modal_open():
            return
        tid = self.selected_tid
        doc = self.ctx.store.load_requirements_safe().get(tid or "")
        if not tid or doc is None:
            self.notify("Select a requirement first.", severity="warning")
            return
        self.push_screen(RequirementScreen("edit", tid, doc), self._after_requirement)

    def _after_requirement(self, result) -> None:
        if not result:
            return
        tid, text = result
        store = self.ctx.store
        try:
            reqs = store.load_requirements()
        except Exception:
            reqs = {}
        reqs[tid] = text
        store.write_requirements(reqs, via="tui")
        self._pending_select = tid
        self._dirty = True
        self.notify(f"{tid} saved to {store.requirements_path.name}. The watcher will emit the event.", title="Requirement written")

    def action_upload(self) -> None:
        if not self._modal_open():
            self.push_screen(UploadScreen(str(self.ctx.store.requirements_path)), self._after_upload)

    def _after_upload(self, path) -> None:
        if not path:
            return
        try:
            self.ctx.set_requirements_file(Path(path))
        except Exception as exc:
            self.notify(f"Could not load {path}: {exc}", severity="error")
            return
        self.selected_tid = None
        self._buf.clear()
        self._dirty = True
        self.query_one("#producers", Static).update(self._producers_text())
        self.notify(f"Loaded {path}", title="Requirements uploaded")

    def action_simulate(self) -> None:
        """Same effect as `reqreview sim add ...` run in another terminal."""
        if self._modal_open():
            return
        store = self.ctx.store
        try:
            reqs = store.load_requirements()
        except Exception:
            self.notify("requirements file is not readable.", severity="error")
            return
        pool = [r for r in SIM_POOL if r["id"] not in reqs]
        if not pool:
            self.notify("All simulated requirements were already added.", severity="warning")
            return
        r = pool[0]
        reqs[r["id"]] = r["text"]
        store.write_requirements(reqs, via="cli")
        self._pending_select = r["id"]
        self._dirty = True
        self.query_one("#tabs", TabbedContent).active = "events"
        self.notify(f"Simulated external webhook: added {r['id']} (no click, the loop starts by itself).",
                    title="Fake Jira")

    def action_analyse(self) -> None:
        if self._modal_open():
            return
        self.query_one("#tabs", TabbedContent).active = "improve"
        self.query_one("#btn-analyse", Button).press()

    def action_theme(self) -> None:
        try:
            self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
        except Exception:
            self.dark = not self.dark

    def action_help(self) -> None:
        if not self._modal_open():
            self.push_screen(HelpScreen())

    # ---- buttons -------------------------------------------------------------------------------
    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        actions = {"btn-upload": self.action_upload, "btn-review": self.action_review,
                   "btn-approve": self.action_approve, "btn-add": self.action_add,
                   "btn-edit": self.action_edit, "btn-sim": self.action_simulate}
        fn = actions.get(event.button.id or "")
        if fn:
            fn()

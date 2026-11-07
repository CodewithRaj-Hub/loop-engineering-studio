"""Tab 3: the hill-climb loop. batch -> pass-rate chart -> analyse -> human approves -> new version -> re-run."""
from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Label, ProgressBar, Static

from ..hillclimb import (
    RUBRIC_ITERATIONS,
    HarnessChange,
    analyse,
    apply_change,
    build_summary,
    estimate_calls,
    load_proposal,
    run_batch,
)
from .screens import ConfirmScreen
from .widgets.diff_view import DiffView
from .widgets.pass_rate_chart import PassRateChart


class ImprovePane(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._proposal: Optional[tuple[str, HarnessChange]] = None   # (version it was derived from, change)

    @property
    def ctx(self):
        return self.app.ctx

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="imp-left"):
                yield Label("Harness versions (AGENTS.md)", classes="title")
                yield DataTable(id="ver-table", cursor_type="row")
                yield Static("", id="imp-hint")
                with Horizontal(classes="btnrow"):
                    yield Button("Run batch", id="btn-batch", variant="primary")
                    yield Button("Analyse", id="btn-analyse")
                with Horizontal(classes="btnrow"):
                    yield Button("Activate", id="btn-activate")
                yield ProgressBar(id="imp-progress", total=100, show_eta=False)
                yield Static("", id="imp-status")
            with Vertical(id="imp-center"):
                yield Label("First-attempt pass rate per criterion", classes="title")
                with VerticalScroll():
                    yield PassRateChart(id="chart")
                    yield Label("Proposed change", classes="title")
                    yield Static("Run a batch, then Analyse to get a proposal.", id="proposal")
            with Vertical(id="imp-right"):
                yield Label("AGENTS.md diff (selected version \u2192 proposed)", classes="title")
                with VerticalScroll():
                    yield DiffView(id="imp-diff")
                with Horizontal(classes="btnrow"):
                    yield Button("Approve change", id="btn-approve", variant="success")
                    yield Button("Reject", id="btn-reject", variant="error")

    def on_mount(self) -> None:
        t = self.query_one("#ver-table", DataTable)
        t.add_column("Version", key="v", width=8)
        t.add_column("Active", key="a", width=7)
        t.add_column("Batch (first-pass satisfied)", key="b")
        self.refresh_all()

    # ---- helpers --------------------------------------------------------------
    def selected_version(self) -> str:
        t = self.query_one("#ver-table", DataTable)
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return self.ctx.store.active_harness()

    def _status(self, text: str) -> None:
        self.query_one("#imp-status", Static).update(text)

    def refresh_all(self) -> None:
        store = self.ctx.store
        active = store.active_harness()
        t = self.query_one("#ver-table", DataTable)
        keep = self.selected_version() if t.row_count else active
        t.clear()
        versions = store.harness_versions()
        for v in versions:
            s = store.load_summary(v)
            batch = f"{s['first_attempt_satisfied']}/{s['n_runs']}" if s else "\u2014"
            t.add_row(Text(v, style="bold"), Text("\u25cf active", style="green") if v == active else "", batch, key=v)
        if keep in versions:
            t.move_cursor(row=t.get_row_index(keep))
        batches = {s["label"]: s["first_attempt_pass_rate"] for s in store.list_summaries()}
        self.query_one(PassRateChart).set_data(batches)
        sel = self.selected_version()
        has_batch = store.load_summary(sel) is not None
        self.query_one("#btn-analyse", Button).disabled = not has_batch
        self.query_one("#btn-activate", Button).disabled = sel == active
        self.query_one("#btn-approve", Button).disabled = self._proposal is None
        self.query_one("#btn-reject", Button).disabled = self._proposal is None
        n = len(store.load_requirements_safe())
        self.query_one("#imp-hint", Static).update(
            Text(f"Batch = all {n} requirements with the selected version, ~{estimate_calls(n)} model calls (estimate)."
                 if not self.ctx.demo else f"Demo: batch of {n} requirements replays instantly, no API calls.",
                 style="dim"))

    @on(DataTable.RowHighlighted, "#ver-table")
    def _version_moved(self) -> None:
        store = self.ctx.store
        sel = self.selected_version()
        self.query_one("#btn-analyse", Button).disabled = store.load_summary(sel) is None
        self.query_one("#btn-activate", Button).disabled = sel == store.active_harness()

    # ---- batch ----------------------------------------------------------------
    @on(Button.Pressed, "#btn-batch")
    def _batch_pressed(self) -> None:
        d = self.ctx.dispatcher
        if d.is_busy() or d.snapshot()["queued"]:
            self.app.notify("A review is running or queued. Wait for it to finish, then run the batch.", severity="warning")
            return
        version = self.selected_version()
        n = len(self.ctx.store.load_requirements_safe())
        existing = self.ctx.store.load_summary(version)
        msg = (f"Run all {n} requirements with harness {version}?\n\n"
               + ("This is a demo replay: no API calls." if self.ctx.demo else
                  f"Estimated ~{estimate_calls(n)} model calls with up to {RUBRIC_ITERATIONS} grading passes each.\n"
                  f"Quota: {getattr(self.app, 'quota_text', 'n/a')}\n"
                  "Free tier is ~20 requests/min, so this takes a while."))
        if existing:
            msg += f"\n\nResults for {version} exist; finished documents are reused (delete data/history/{version} to redo)."
        self.app.push_screen(ConfirmScreen(msg, "Run batch"), lambda ok: self._start_batch(version) if ok else None)

    def _start_batch(self, version: str) -> None:
        self._status(f"Running batch for {version}\u2026")
        self.query_one("#imp-progress", ProgressBar).update(total=max(1, len(self.ctx.store.load_requirements_safe())), progress=0)
        self._batch_worker(version)

    @work(thread=True, exclusive=True, group="batch")
    def _batch_worker(self, version: str) -> None:
        ctx = self.ctx
        ctx.dispatcher.pause()
        try:
            tickets = ctx.store.load_requirements()
            ctx.log.append("harness.batch_started", version, "analyst", {"label": version, "docs": len(tickets)})

            def prog(done: int, total: int, tid: str) -> None:
                self.app.call_from_thread(self._on_progress, done, total, tid)

            summary = run_batch(ctx.store, ctx.runner, label=version, harness_version=version,
                                tickets=tickets, on_progress=prog)
            ctx.log.append("harness.batch_completed", version, "analyst",
                           {"label": version, "docs": summary["n_runs"],
                            "first_pass_rates": {k: round(v, 2) for k, v in summary["first_attempt_pass_rate"].items()}})
            self.app.call_from_thread(self._status, f"Batch {version} done: {summary['first_attempt_satisfied']}/"
                                                    f"{summary['n_runs']} satisfied on the first attempt.")
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, f"Batch failed: {exc}", severity="error")
            self.app.call_from_thread(self._status, f"Batch failed: {exc}")
        finally:
            ctx.dispatcher.resume()
            self.app.call_from_thread(self.refresh_all)

    def _on_progress(self, done: int, total: int, tid: str) -> None:
        self.query_one("#imp-progress", ProgressBar).update(total=total, progress=done)
        self._status(f"Batch {done}/{total}: {tid}")

    # ---- analyse --------------------------------------------------------------
    @on(Button.Pressed, "#btn-analyse")
    def _analyse_pressed(self) -> None:
        version = self.selected_version()
        if not self.ctx.store.load_summary(version):
            self.app.notify("Run the batch for this version first.", severity="warning")
            return
        self._status(f"Analyst reading the {version} batch\u2026")
        self._analyse_worker(version)

    @work(thread=True, exclusive=True, group="analyse")
    def _analyse_worker(self, version: str) -> None:
        try:
            change, _md = analyse(self.ctx.store, version, live=not self.ctx.demo, speed=self.ctx.speed)
        except Exception as exc:
            self.app.call_from_thread(self._status, f"No proposal: {exc}")
            self.app.call_from_thread(self.app.notify, f"No proposal: {exc}", severity="warning")
            return
        self.ctx.log.append("harness.proposal_ready", version, "analyst",
                            {"label": version, "target": change.target, "issue": change.issue[:80]})
        self.app.call_from_thread(self._show_proposal, version, change)

    def _show_proposal(self, version: str, change: HarnessChange) -> None:
        self._proposal = (version, change)
        t = Text()
        t.append("Target: ", style="bold")
        t.append(f"{change.target}   ")
        t.append("Section: ", style="bold")
        t.append(f"{change.section}\n\n")
        t.append("Issue\n", style="bold cyan")
        t.append(change.issue + "\n\n")
        t.append("Recurrence\n", style="bold cyan")
        t.append(change.recurrence + "\n\n")
        t.append("Evidence (verbatim from the runs)\n", style="bold cyan")
        for e in change.evidence:
            t.append(f"  \u2022 {e}\n", style="italic")
        if change.rejected:
            t.append("\nConsidered and rejected\n", style="bold cyan")
            for r in change.rejected:
                t.append(f"  \u2022 {r}\n", style="dim")
        self.query_one("#proposal", Static).update(t)
        diff = self.query_one(DiffView)
        try:
            before = self.ctx.store.harness_text(version)
            diff.show(before, apply_change(change, before), f"AGENTS.{version}.md", "proposed")
        except ValueError as exc:
            diff.show_message(str(exc))
        self._status("Proposal ready. Read the evidence and the diff, then approve or reject.")
        self.refresh_all()

    # ---- human gate 2 ------------------------------------------------------------
    @on(Button.Pressed, "#btn-approve")
    def _approve(self) -> None:
        if not self._proposal:
            return
        version, change = self._proposal
        store = self.ctx.store
        try:
            new_text = apply_change(change, store.harness_text(version))
        except ValueError as exc:
            self.app.notify(str(exc), severity="warning")
            return
        nv = store.add_version(new_text)
        store.activate(nv)
        self.ctx.log.append("harness.change_approved", version, "human", {"label": version, "version": nv})
        self.ctx.log.append("harness.activated", nv, "human", {"version": nv})
        self._proposal = None
        self.query_one("#proposal", Static).update("Approved. Run the batch for the new version to compare.")
        self.query_one(DiffView).show_message("")
        self._status(f"{nv} created and activated. Select {nv} and press Run batch to measure the effect.")
        self.refresh_all()
        self.app.refresh_subtitle()
        self.app.notify(f"{nv} created from {version}. Run the batch to compare.", title="Harness approved")

    @on(Button.Pressed, "#btn-reject")
    def _reject(self) -> None:
        if not self._proposal:
            return
        version, _ = self._proposal
        self.ctx.log.append("harness.change_rejected", version, "human", {"label": version})
        self._proposal = None
        self.query_one("#proposal", Static).update("Rejected. The harness is unchanged.")
        self.query_one(DiffView).show_message("")
        self._status("Proposal rejected.")
        self.refresh_all()

    @on(Button.Pressed, "#btn-activate")
    def _activate(self) -> None:
        v = self.selected_version()
        self.ctx.store.activate(v)
        self.ctx.log.append("harness.activated", v, "human", {"version": v})
        self.refresh_all()
        self.app.refresh_subtitle()
        self.app.notify(f"Harness {v} is now active for new reviews.")

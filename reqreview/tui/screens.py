"""Modal dialogs: upload, add/edit requirement, confirm, help."""
from __future__ import annotations

import re

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static, TextArea

ID_RE = re.compile(r"^[A-Za-z][\w.-]*$")

HELP = """\
[b]Loop Engineering Studio[/b]

[b]Four loops[/b]
  [cyan]Agent loop[/cyan]         the review agent works stage by stage (trace tree, one tool call per turn)
  [cyan]Verification loop[/cyan]  a grader reads /analysis.md against a rubric and sends the agent back
  [cyan]Event-driven loop[/cyan]  changing requirements.json emits an event -> queue -> review -> store
  [cyan]Hill-climb loop[/cyan]    batch results -> proposed AGENTS.md change -> you approve -> re-run, compare

[b]Two human gates[/b]  Approve a review (p)  ·  Approve a harness change (Improve tab)

[b]Keys[/b]
  1 2 3   tabs          u  upload JSON         r  review selected
  a  add requirement    e  edit selected       p  approve selected review
  s  simulate an external webhook (writes requirements.json like `reqreview sim add`)
  i  analyse (Improve tab)    t  theme    ?  help    q  quit

[b]Try it[/b]  In a second terminal:  reqreview sim add NEW-1 --text "REQUIREMENT: Demo\\n\\nIt should be fast."
and watch the Event Loop tab react with no click.
"""


class _Modal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Close")]

    def action_close(self) -> None:
        self.dismiss(None)


class UploadScreen(_Modal):
    def __init__(self, current: str = "requirements.json"):
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Upload requirements JSON", classes="title")
            yield Static("Path to a JSON file shaped like {\"AUTH-142\": \"document text\", ...}. "
                         "Add/Edit will write back to this same file.", classes="hint")
            yield Input(value=self._current, placeholder="path/to/requirements.json", id="path")
            with Horizontal(classes="btnrow"):
                yield Button("Load", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(self.query_one("#path", Input).value.strip() or None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class RequirementScreen(_Modal):
    def __init__(self, mode: str, tid: str = "", text: str = ""):
        super().__init__()
        self.mode, self.tid = mode, tid
        self.text = text or "REQUIREMENT: \n\n"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Add requirement" if self.mode == "add" else f"Edit {self.tid}", classes="title")
            yield Static("Saving writes requirements.json. The watcher emits the event; the review starts by itself.",
                         classes="hint")
            yield Input(value=self.tid, placeholder="TICKET-123", id="rid", disabled=self.mode == "edit")
            yield TextArea(self.text, id="rtext")
            yield Static("", id="rerr", classes="error")
            with Horizontal(classes="btnrow"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        tid = self.query_one("#rid", Input).value.strip()
        text = self.query_one("#rtext", TextArea).text
        if not ID_RE.match(tid):
            self.query_one("#rerr", Static).update("ID must start with a letter, e.g. PAY-310")
            return
        if len(text.strip()) < 10:
            self.query_one("#rerr", Static).update("Write the requirement text first.")
            return
        self.dismiss((tid, text if text.endswith("\n") else text + "\n"))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(_Modal):
    def __init__(self, message: str, yes: str = "Yes"):
        super().__init__()
        self.message, self.yes = message, yes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Please confirm", classes="title")
            yield Static(self.message)
            with Horizontal(classes="btnrow"):
                yield Button(self.yes, variant="primary", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed, "#yes")
    def _y(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _n(self) -> None:
        self.dismiss(False)

    def action_close(self) -> None:
        self.dismiss(False)


class HelpScreen(_Modal):
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            with VerticalScroll():
                yield Static(HELP)
            with Horizontal(classes="btnrow"):
                yield Button("Close", id="close", variant="primary")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)

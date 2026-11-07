"""Unified diff rendered with colours (before -> after)."""
from __future__ import annotations

import difflib

from rich.text import Text
from textual.widgets import Static


class DiffView(Static):
    def show(self, before: str, after: str, before_name: str = "before", after_name: str = "after") -> None:
        lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), before_name, after_name,
                                          lineterm="", n=2))
        if not lines:
            self.update(Text("No differences.", style="dim"))
            return
        t = Text()
        for ln in lines:
            style = ("green" if ln.startswith("+") and not ln.startswith("+++") else
                     "red" if ln.startswith("-") and not ln.startswith("---") else
                     "cyan" if ln.startswith("@@") else "bold" if ln.startswith(("---", "+++")) else "")
            t.append(ln + "\n", style=style)
        self.update(t)

    def show_message(self, message: str) -> None:
        self.update(Text(message, style="dim"))

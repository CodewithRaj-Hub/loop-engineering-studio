from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable


class RequirementsTable(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_column("ID", key="id", width=11)
        self.add_column("Title", key="title", width=24)
        self.add_column("Status", key="status", width=17)
        self.add_column("Last", key="last", width=9)
        self.add_column("Harness", key="harness", width=7)

    def selected_key(self) -> str | None:
        try:
            return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value
        except Exception:
            return None

    def reload(self, rows: list[tuple]) -> None:
        """rows: (ticket_id, title, StatusInfo, last_run_clock, harness_version)."""
        keep = self.selected_key()
        self.clear()
        keys = []
        for tid, title, st, last, harness in rows:
            chip = Text(f" {st.label} ", style=f"bold {st.fg} on {st.bg}")
            self.add_row(Text(tid, style="bold"), title[:24], chip, last or "\u2014", harness or "", key=tid)
            keys.append(tid)
        if keep in keys:
            self.move_cursor(row=self.get_row_index(keep))

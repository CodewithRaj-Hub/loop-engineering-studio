"""First-attempt pass rate per criterion, one bar per harness version, with v1 -> vN deltas."""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

WIDTH = 24


def _bar(rate: float) -> Text:
    filled = round(rate * WIDTH)
    colour = "green" if rate >= 0.8 else "yellow" if rate >= 0.5 else "red"
    t = Text()
    t.append("\u2588" * filled, style=colour)
    t.append("\u2591" * (WIDTH - filled), style="dim")
    t.append(f" {rate:>4.0%}", style=f"bold {colour}")
    return t


class PassRateChart(Static):
    def set_data(self, batches: dict[str, dict[str, float]]) -> None:
        if not batches:
            self.update(Text("Run a batch to see first-attempt pass rates.", style="dim"))
            return
        labels = list(batches)
        criteria: list[str] = []
        for rates in batches.values():
            for name in rates:
                if name not in criteria:
                    criteria.append(name)
        t = Text()
        for name in criteria:
            t.append(f"{name}\n", style="bold")
            for lab in labels:
                if name in batches[lab]:
                    t.append(f"  {lab:<4}", style="cyan")
                    t.append_text(_bar(batches[lab][name]))
                    t.append("\n")
            if len(labels) >= 2 and name in batches[labels[0]] and name in batches[labels[-1]]:
                delta = (batches[labels[-1]][name] - batches[labels[0]][name]) * 100
                arrow, colour = ("\u25b2", "green") if delta > 0 else ("\u25bc", "red") if delta < 0 else ("=", "dim")
                t.append(f"  {arrow} {delta:+.0f} pp ({labels[0]} \u2192 {labels[-1]})\n", style=colour)
            t.append("\n")
        self.update(t)

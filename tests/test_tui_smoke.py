"""Smoke test: the app boots in demo mode and a review streams into the trace tree."""
import asyncio
import shutil
from pathlib import Path

import pytest

pytest.importorskip("textual")

from reqreview.context import build_context
from reqreview.tui.app import StudioApp
from reqreview.tui.widgets.trace_tree import TraceTree


def test_boot_and_review_in_demo_mode(tmp_path):
    req = Path(__file__).parent.parent / "requirements.json"

    async def scenario():
        ctx = build_context(req, tmp_path / "data", demo=True, speed=1000)
        app = StudioApp(ctx)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.5)
            assert app.selected_tid is not None
            await pilot.press("r")
            for _ in range(60):
                await pilot.pause(0.2)
                if ctx.store.latest(app.selected_tid):
                    break
            assert ctx.store.latest(app.selected_tid) is not None
            await pilot.pause(0.6)
            assert len(app.query_one(TraceTree).root.children) > 0

    asyncio.run(scenario())
    shutil.rmtree(tmp_path, ignore_errors=True)

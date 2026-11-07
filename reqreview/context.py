"""Wires store, event log, runner, dispatcher and watcher together (shared by the CLI and the TUI)."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diag import DiagLog
from .events import Dispatcher, EventLog, RequirementWatcher
from .runner import LiveRunner, ReplayRunner
from .store import ASSETS, Store
from .util import sha256_text


@dataclass
class AppContext:
    store: Store
    log: EventLog
    runner: Any
    dispatcher: Dispatcher
    watcher: RequirementWatcher
    demo: bool
    speed: float
    diag: DiagLog

    def start(self) -> None:
        self.dispatcher.start()      # subscribe first, so the watcher's catch-up scan is processed
        self.watcher.start()

    def stop(self) -> None:
        self.watcher.stop()
        self.dispatcher.stop()

    def set_requirements_file(self, path: Path) -> None:
        """Upload: validate, point the system of record at the file, baseline the snapshot, rewatch."""
        path = Path(path).expanduser()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
            raise ValueError("expected a JSON object like {\"AUTH-142\": \"document text\", ...}")
        self.watcher.stop()
        self.store.set_requirements_path(path)
        self.store.save_snapshot({k: sha256_text(v) for k, v in data.items()})
        self.watcher.start()


def build_context(requirements: Path | str, data_dir: Path | str, demo: bool = False, speed: float = 1.0,
                  rubric_iterations: int = 3) -> AppContext:
    data_dir = Path(data_dir)
    req = Path(requirements)
    if demo:
        data_dir = data_dir / "demo"
        if data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)   # demo always starts clean and never touches live data
        data_dir.mkdir(parents=True, exist_ok=True)
        seed = req if req.exists() else ASSETS / "requirements.sample.json"
        shutil.copy(seed, data_dir / "requirements.json")
        req = data_dir / "requirements.json"
    elif not req.exists():
        raise FileNotFoundError(f"{req} not found. Pass --requirements PATH or run with --demo.")
    store = Store(req, data_dir)
    store.ensure()
    log = EventLog(store.events_path)
    if demo:
        runner: Any = ReplayRunner(store, demo_dir=Path("demo"), speed=speed)
    else:
        runner = LiveRunner(store, rubric_iterations=rubric_iterations)
    dispatcher = Dispatcher(log, store, runner)
    diag = DiagLog(store.data_dir / "logs")
    log.subscribe(diag.event)
    dispatcher.diag = diag
    watcher = RequirementWatcher(store, log)
    return AppContext(store, log, runner, dispatcher, watcher, demo, speed, diag)

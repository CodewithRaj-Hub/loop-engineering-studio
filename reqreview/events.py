"""The event-driven loop.

    requirements.json (system of record)
        -> RequirementWatcher  (snapshot diff by sha256)  -> requirement.created/updated/removed
        -> EventLog (events.jsonl, append-only)
        -> Dispatcher (debounce, hash idempotency, approved-skip, one worker, supersede)
        -> Runner -> store -> review.* events

Producers (TUI buttons, `reqreview sim`, an external editor) only ever change requirements.json (D-06).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Optional

from .diag import explain
from .runner import Job
from .util import new_id, sha256_text, utc_now

# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


@dataclass
class Event:
    id: str
    ts: str
    type: str
    subject: str
    source: str
    correlation_id: str
    payload: dict[str, Any] = field(default_factory=dict)


_EVENT_FIELDS = {f.name for f in fields(Event)}


class EventLog:
    """Append-only JSONL. In-process subscribers are called synchronously; the TUI tails the file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._subs: list[Callable[[Event], None]] = []

    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._subs.append(fn)

    def append(self, type: str, subject: str, source: str, payload: Optional[dict] = None,
               correlation_id: Optional[str] = None) -> Event:
        ev = Event(new_id("evt"), utc_now(), type, subject, source, correlation_id or new_id("cor"), payload or {})
        line = json.dumps(asdict(ev), ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        for fn in list(self._subs):
            try:
                fn(ev)
            except Exception:
                pass
        return ev

    def read_from(self, offset: int = 0) -> tuple[list[Event], int]:
        """Events written after `offset` (bytes). Only complete lines are consumed."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return [], 0
        if offset > size:  # file was replaced
            offset = 0
        with self.path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        end = data.rfind(b"\n")
        if end == -1:
            return [], offset
        chunk = data[: end + 1]
        events: list[Event] = []
        for raw in chunk.splitlines():
            try:
                d = json.loads(raw.decode("utf-8"))
                events.append(Event(**{k: v for k, v in d.items() if k in _EVENT_FIELDS}))
            except Exception:
                continue
        return events, offset + len(chunk)


# ---------------------------------------------------------------------------
# Watcher: file change -> events
# ---------------------------------------------------------------------------


class RequirementWatcher:
    def __init__(self, store: Any, log: EventLog):
        self.store = store
        self.log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._scan_lock = threading.Lock()

    def start(self) -> None:
        self.scan()  # catch up with changes made while the app was closed
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), daemon=True, name="req-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def set_path(self, path: Path | str) -> None:
        self.stop()
        self.store.set_requirements_path(path)
        self.start()

    def _read(self) -> Optional[dict[str, str]]:
        for _ in range(5):  # tolerate a half-written file or a Windows lock
            try:
                return self.store.load_requirements()
            except Exception:
                time.sleep(0.1)
        return None

    def scan(self) -> None:
        """Diff requirements.json against the last snapshot and emit one chain per changed ticket."""
        with self._scan_lock:
            reqs = self._read()
            if reqs is None:
                self.log.append("requirement.invalid", self.store.requirements_path.name, "watcher",
                                {"error": "requirements file is not valid JSON {id: text}"})
                return
            new = {tid: sha256_text(doc) for tid, doc in reqs.items()}
            snap = self.store.load_snapshot()
            if snap is None:  # first ever run: baseline silently, do not review everything at once
                self.store.save_snapshot(new)
                return
            via = self.store.recent_write_via() or "external"
            changes: list[tuple[str, str, str]] = []
            for tid, h in new.items():
                if tid not in snap:
                    changes.append(("created", tid, h))
                elif snap[tid] != h:
                    changes.append(("updated", tid, h))
            for tid in snap:
                if tid not in new:
                    changes.append(("removed", tid, ""))
            if changes:
                self.store.save_snapshot(new)
            for kind, tid, h in changes:
                self.log.append(f"requirement.{kind}", tid, "watcher", {"content_hash": h, "via": via})

    def _loop(self, stop: threading.Event) -> None:
        path = Path(self.store.requirements_path)
        try:
            from watchfiles import watch
            for changes in watch(str(path.parent), stop_event=stop, debounce=300, step=100,
                                 rust_timeout=1000, yield_on_timeout=False):
                if any(Path(p).name == path.name for _, p in changes):
                    self.scan()
            return
        except Exception:
            pass  # watchfiles missing or failed: fall back to polling
        last = None
        while not stop.wait(1.0):
            try:
                mt = path.stat().st_mtime
            except OSError:
                continue
            if mt != last:
                last = mt
                self.scan()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _noop(*_a: Any, **_k: Any) -> None:
    return None


@dataclass
class Hooks:
    """UI callbacks, invoked from worker threads (the TUI marshals them with call_from_thread)."""

    on_step: Callable = _noop      # (job, step)
    on_grade: Callable = _noop     # (job, grading_pass)
    on_state: Callable = _noop     # (ticket_id)


class Dispatcher:
    def __init__(self, log: EventLog, store: Any, runner: Any, debounce_s: float = 1.5):
        self.log, self.store, self.runner, self.debounce_s = log, store, runner, debounce_s
        self.hooks = Hooks()
        self.diag: Any = None          # optional DiagLog (readable studio.log + errors.log)
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._pending: dict[str, dict] = {}
        self._queue: list[Job] = []
        self._running: Optional[Job] = None
        self._live: dict[str, tuple[str, int]] = {}
        self._paused = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        log.subscribe(self.on_event)

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._threads:
            return
        for target, name in ((self._ticker, "dispatch-ticker"), (self._worker, "dispatch-worker")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    def pause(self) -> None:
        with self._cv:
            self._paused = True

    def resume(self) -> None:
        with self._cv:
            self._paused = False
            self._cv.notify_all()

    # ---- introspection (UI) -------------------------------------------------
    def live_of(self, tid: str) -> Optional[tuple[str, int]]:
        with self._lock:
            return self._live.get(tid)

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            return {
                "running": self._running,
                "queued": [j.ticket_id for j in self._queue],
                "pending": [(t, max(0.0, p["deadline"] - now)) for t, p in self._pending.items()],
                "paused": self._paused,
            }

    def is_busy(self) -> bool:
        with self._lock:
            return self._running is not None

    # ---- inputs --------------------------------------------------------------
    def on_event(self, ev: Event) -> None:
        if ev.type in ("requirement.created", "requirement.updated"):
            with self._lock:
                old = self._pending.get(ev.subject)
                if old:  # coalesce: the older trigger is dropped, and we say so
                    self.log.append("review.skipped", ev.subject, "dispatcher",
                                    {"reason": "debounced"}, old["cid"])
                self._pending[ev.subject] = {
                    "deadline": time.monotonic() + self.debounce_s,
                    "cid": ev.correlation_id,
                    "reason": ev.type,
                }
        elif ev.type == "requirement.removed":
            with self._lock:
                self._pending.pop(ev.subject, None)
                self._queue = [j for j in self._queue if j.ticket_id != ev.subject]
                self._live.pop(ev.subject, None)
            self._notify(ev.subject)

    def submit_manual(self, tid: str, source: str = "tui") -> bool:
        """Review button: bypasses debounce and the unchanged/approved checks."""
        doc = self.store.load_requirements_safe().get(tid)
        if doc is None:
            return False
        with self._lock:
            if (self._running and self._running.ticket_id == tid) or any(j.ticket_id == tid for j in self._queue):
                return False
        job = Job(tid, doc, "manual", new_id("cor"), self.store.active_harness(), sha256_text(doc))
        self._enqueue(job, source)
        return True

    # ---- internals -------------------------------------------------------------
    def _ticker(self) -> None:
        while not self._stop.wait(0.2):
            now = time.monotonic()
            with self._lock:
                due = [t for t, p in self._pending.items() if p["deadline"] <= now]
                items = [(t, self._pending.pop(t)) for t in due]
            for tid, p in items:
                try:
                    self._consider(tid, p["cid"], p["reason"])
                except Exception:
                    pass

    def _consider(self, tid: str, cid: str, reason: str) -> None:
        doc = self.store.load_requirements_safe().get(tid)
        if doc is None:
            return
        h = sha256_text(doc)
        if h == self.store.last_reviewed_hash(tid):
            self.log.append("review.skipped", tid, "dispatcher", {"reason": "unchanged"}, cid)
            return
        if self.store.is_approved_latest(tid):  # D-08: Approved work is not silently re-opened
            self.log.append("review.skipped", tid, "dispatcher", {"reason": "approved"}, cid)
            self._notify(tid)
            return
        self._enqueue(Job(tid, doc, reason, cid, self.store.active_harness(), h), "dispatcher")

    def _enqueue(self, job: Job, source: str) -> None:
        with self._cv:
            for i, queued in enumerate(self._queue):
                if queued.ticket_id == job.ticket_id:  # newer content replaces the waiting job
                    self._queue[i] = job
                    break
            else:
                self._queue.append(job)
            position = len(self._queue)
            self._live[job.ticket_id] = ("queued", 0)
            self._cv.notify_all()
        self.log.append("review.queued", job.ticket_id, "dispatcher",
                        {"position": position, "reason": job.reason, "via": source}, job.correlation_id)
        self._notify(job.ticket_id)

    def _notify(self, tid: str) -> None:
        self._safe(self.hooks.on_state, tid)

    @staticmethod
    def _safe(fn: Callable, *a: Any) -> None:
        try:
            fn(*a)
        except Exception:
            pass

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while (not self._queue or self._paused) and not self._stop.is_set():
                    self._cv.wait(0.3)
                if self._stop.is_set():
                    break
                job = self._queue.pop(0)
                self._running = job
            try:
                self._run_job(job)
            finally:
                with self._lock:
                    self._running = None
                    self._live.pop(job.ticket_id, None)
                self._notify(job.ticket_id)

    def _run_job(self, job: Job) -> None:
        tid, cid = job.ticket_id, job.correlation_id
        run_n = self.store.next_run_n(tid)
        started = utc_now()
        with self._lock:
            self._live[tid] = ("reviewing", 1)
        self.log.append("review.started", tid, "dispatcher",
                        {"run_n": run_n, "harness_version": job.harness_version,
                         "model": getattr(self.runner, "model_name", "?")}, cid)
        self._notify(tid)

        def on_step(step: Any) -> None:
            if self.diag:
                self.diag.step(tid, step)
            self._safe(self.hooks.on_step, job, step)

        def on_grade(gp: Any) -> None:
            ok = gp.result == "satisfied"
            self.log.append("grade.passed" if ok else "grade.failed", tid, "grader",
                            {"iteration": gp.iteration, "failed_criteria": gp.failed}, cid)
            if not ok:
                with self._lock:
                    self._live[tid] = ("verifying", gp.iteration)
            if self.diag:
                self.diag.grade(tid, gp)
            self._safe(self.hooks.on_grade, job, gp)
            self._notify(tid)

        def on_event(kind: str, payload: dict) -> None:
            self.log.append("llm.retry", tid, "agent", {"kind": kind, **payload}, cid)

        try:
            result = self.runner.run(job, run_n, on_step, on_grade, on_event)
            run = result.run
            superseded = self.store.current_hash(tid) != job.content_hash
            meta = {
                "ticket_id": tid, "run_n": run_n, "req_hash": job.content_hash,
                "harness_version": job.harness_version, "model": result.model,
                "grader_model": result.grader_model, "started_at": started, "finished_at": utc_now(),
                "correlation_id": cid, "superseded": superseded, "reason": job.reason,
                "synthetic": result.synthetic,
            }
            self.store.save_run(tid, run, meta)
            self.store.clear_error(tid)
            self.log.append("review.completed", tid, "dispatcher",
                            {"status": getattr(run, "status", ""), "turns": run.turns,
                             "passes": len(getattr(run, "grading", [])), "run_n": run_n,
                             "superseded": superseded}, cid)
        except Exception as exc:  # the job fails, the loop keeps running
            info = explain(exc)
            raw = f"{type(exc).__name__}: {exc}"
            self.store.save_error(tid, raw[:2000], job.content_hash, info.as_dict())
            if self.diag:
                self.diag.error(tid, exc, info)
            self.log.append("review.failed", tid, "dispatcher",
                            {"error": raw[:800], "title": info.title, "cause": info.cause, "fix": info.fix}, cid)

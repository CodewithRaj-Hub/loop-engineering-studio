"""Local persistence: requirements (system of record), review runs, approvals, harness versions.

Everything is plain files under `data_dir` so a reviewer can inspect it:

    data/events.jsonl                 append-only event log
    data/snapshot.json                last-seen {ticket_id: content hash}
    data/last_write.json              who last wrote requirements.json (tui | cli)
    data/reviews/<ID>/run_<n>.json    VerificationRun + meta
    data/reviews/<ID>/approval.json   human approval for one run / content hash
    data/reviews/<ID>/error.json      last crash, if any
    data/harness/AGENTS.v<N>.md       immutable harness versions, active.txt = current
    data/history/<label>/...          hill-climb batches, proposals
"""
from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .agent import load_run_with_meta
from .util import atomic_write_text, sha256_text, utc_now

ASSETS = Path(__file__).parent / "assets"

# key -> (label, foreground, background)
STATUS_STYLES: dict[str, tuple[str, str, str]] = {
    "new": ("New", "white", "grey37"),
    "queued": ("Queued", "white", "blue"),
    "reviewing": ("Reviewing", "black", "yellow"),
    "verifying": ("Verifying", "black", "dark_orange"),
    "ready": ("Ready", "black", "green"),
    "attention": ("Needs attention", "white", "red"),
    "approved": ("Approved ✔", "black", "bright_green"),
    "stale": ("Stale", "white", "purple"),
    "error": ("Error ✖", "white", "red3"),
}


@dataclass(frozen=True)
class StatusInfo:
    key: str
    label: str
    fg: str
    bg: str


def _status(key: str, label: Optional[str] = None) -> StatusInfo:
    base, fg, bg = STATUS_STYLES[key]
    return StatusInfo(key, label or base, fg, bg)


@dataclass
class RunView:
    n: int
    run: Any
    meta: dict
    path: Path

    @property
    def status(self) -> str:
        return getattr(self.run, "status", "") or ""

    @property
    def passes(self) -> int:
        return len(getattr(self.run, "grading", []) or [])


def _safe(tid: str) -> str:
    return re.sub(r"[^\w.-]", "_", tid)


def _version_num(v: str) -> int:
    m = re.search(r"(\d+)", v)
    return int(m.group(1)) if m else 0


class Store:
    def __init__(self, requirements_path: Path | str, data_dir: Path | str):
        self.requirements_path = Path(requirements_path)
        self.data_dir = Path(data_dir)
        self._cache: dict[Path, tuple[float, RunView]] = {}

    # ---- paths -----------------------------------------------------------
    @property
    def events_path(self) -> Path:
        return self.data_dir / "events.jsonl"

    @property
    def snapshot_path(self) -> Path:
        return self.data_dir / "snapshot.json"

    @property
    def marker_path(self) -> Path:
        return self.data_dir / "last_write.json"

    @property
    def reviews_dir(self) -> Path:
        return self.data_dir / "reviews"

    @property
    def harness_dir(self) -> Path:
        return self.data_dir / "harness"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def workspace_root(self) -> Path:
        return self.data_dir / ".run_workspace"

    def ensure(self) -> None:
        for d in (self.data_dir, self.reviews_dir, self.harness_dir, self.history_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.harness_versions():
            shutil.copy(ASSETS / "AGENTS.v1.md", self.harness_dir / "AGENTS.v1.md")
        if not (self.harness_dir / "active.txt").exists():
            atomic_write_text(self.harness_dir / "active.txt", self.harness_versions()[0])

    def set_requirements_path(self, path: Path | str) -> None:
        self.requirements_path = Path(path)

    # ---- requirements (system of record, D-05 / D-06) ----------------------
    def load_requirements(self) -> dict[str, str]:
        data = json.loads(self.requirements_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("requirements file must be a JSON object {ticket_id: document}")
        return {str(k): str(v) for k, v in data.items()}

    def load_requirements_safe(self) -> dict[str, str]:
        try:
            return self.load_requirements()
        except Exception:
            return {}

    def write_requirements(self, reqs: dict[str, str], via: str) -> None:
        """Atomic write. The marker lets the watcher label the source (tui / cli / external)."""
        atomic_write_text(self.marker_path, json.dumps({"via": via, "t": time.time()}))
        atomic_write_text(self.requirements_path, json.dumps(reqs, indent=2, ensure_ascii=False) + "\n")

    def recent_write_via(self, within_s: float = 10.0) -> Optional[str]:
        try:
            m = json.loads(self.marker_path.read_text(encoding="utf-8"))
            if time.time() - float(m.get("t", 0)) <= within_s:
                return str(m.get("via"))
        except Exception:
            pass
        return None

    def current_hash(self, tid: str) -> Optional[str]:
        doc = self.load_requirements_safe().get(tid)
        return sha256_text(doc) if doc is not None else None

    @staticmethod
    def title_of(doc: str) -> str:
        for line in doc.splitlines():
            line = line.strip()
            if line:
                return line.split(":", 1)[1].strip() if line.upper().startswith("REQUIREMENT:") else line
        return ""

    def load_snapshot(self) -> Optional[dict[str, str]]:
        try:
            return json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save_snapshot(self, snap: dict[str, str]) -> None:
        atomic_write_text(self.snapshot_path, json.dumps(snap, indent=2))

    # ---- runs --------------------------------------------------------------
    def _tdir(self, tid: str) -> Path:
        return self.reviews_dir / _safe(tid)

    def load_runs(self, tid: str) -> list[RunView]:
        d = self._tdir(tid)
        out: list[RunView] = []
        if not d.exists():
            return out
        for p in d.glob("run_*.json"):
            m = re.match(r"run_(\d+)\.json$", p.name)
            if not m:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            cached = self._cache.get(p)
            if cached and cached[0] == mt:
                out.append(cached[1])
                continue
            try:
                run, meta = load_run_with_meta(p)
            except Exception:
                continue
            rv = RunView(int(m.group(1)), run, meta, p)
            self._cache[p] = (mt, rv)
            out.append(rv)
        out.sort(key=lambda r: r.n)
        return out

    def latest(self, tid: str) -> Optional[RunView]:
        runs = self.load_runs(tid)
        return runs[-1] if runs else None

    def next_run_n(self, tid: str) -> int:
        runs = self.load_runs(tid)
        return (runs[-1].n + 1) if runs else 1

    def last_reviewed_hash(self, tid: str) -> Optional[str]:
        rv = self.latest(tid)
        return rv.meta.get("req_hash") if rv else None

    def save_run(self, tid: str, run: Any, meta: dict) -> Path:
        n = int(meta["run_n"])
        payload = asdict(run)
        payload["meta"] = meta
        path = self._tdir(tid) / f"run_{n}.json"
        atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
        return path

    # ---- errors ------------------------------------------------------------
    def save_error(self, tid: str, message: str, req_hash: str, info: Optional[dict] = None) -> None:
        atomic_write_text(self._tdir(tid) / "error.json",
                          json.dumps({"ts": utc_now(), "error": message, "req_hash": req_hash, **(info or {})}))

    def load_error(self, tid: str) -> Optional[dict]:
        try:
            return json.loads((self._tdir(tid) / "error.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def clear_error(self, tid: str) -> None:
        (self._tdir(tid) / "error.json").unlink(missing_ok=True)

    # ---- human gate 1: approval --------------------------------------------
    def approve(self, tid: str, run_n: int) -> None:
        rv = next((r for r in self.load_runs(tid) if r.n == run_n), None)
        if rv is None:
            raise ValueError(f"{tid} has no run {run_n}")
        atomic_write_text(self._tdir(tid) / "approval.json", json.dumps(
            {"run_n": run_n, "req_hash": rv.meta.get("req_hash"), "ts": utc_now()}))

    def approval(self, tid: str) -> Optional[dict]:
        try:
            return json.loads((self._tdir(tid) / "approval.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def is_approved_latest(self, tid: str) -> bool:
        """True when the approval covers the content of the most recent review."""
        ap, rv = self.approval(tid), self.latest(tid)
        return bool(ap and rv and ap.get("req_hash") == rv.meta.get("req_hash"))

    # ---- derived status (never stored as a flag) ---------------------------
    def status_of(self, tid: str, live: Optional[tuple[str, int]] = None) -> StatusInfo:
        if live:
            phase, n = live
            if phase == "queued":
                return _status("queued")
            if phase == "verifying":
                return _status("verifying", f"Verifying ({n})")
            return _status("reviewing")
        cur = self.current_hash(tid)
        runs = self.load_runs(tid)
        err = self.load_error(tid)
        if err and err.get("req_hash") == cur and (not runs or err.get("ts", "") > runs[-1].meta.get("finished_at", "")):
            return _status("error")
        if not runs:
            return _status("new")
        latest = runs[-1]
        ap = self.approval(tid)
        if ap and ap.get("req_hash") == cur:
            return _status("approved")
        if latest.meta.get("req_hash") != cur:
            return _status("stale")
        return _status("ready" if latest.status == "satisfied" else "attention")

    # ---- harness versions (human gate 2 lives in the Improve tab) ----------
    def harness_versions(self) -> list[str]:
        if not self.harness_dir.exists():
            return []
        vs = [p.stem.split(".", 1)[1] for p in self.harness_dir.glob("AGENTS.v*.md")]
        return sorted(vs, key=_version_num)

    def active_harness(self) -> str:
        try:
            v = (self.harness_dir / "active.txt").read_text(encoding="utf-8").strip()
            if v:
                return v
        except Exception:
            pass
        vs = self.harness_versions()
        return vs[0] if vs else "v1"

    def activate(self, version: str) -> None:
        if version not in self.harness_versions():
            raise ValueError(f"unknown harness version {version}")
        atomic_write_text(self.harness_dir / "active.txt", version)

    def harness_text(self, version: str) -> str:
        return (self.harness_dir / f"AGENTS.{version}.md").read_text(encoding="utf-8")

    def add_version(self, text: str) -> str:
        """Create the next immutable version. Existing versions are never overwritten."""
        vs = self.harness_versions()
        n = (_version_num(vs[-1]) + 1) if vs else 1
        atomic_write_text(self.harness_dir / f"AGENTS.v{n}.md", text)
        return f"v{n}"

    # ---- hill-climb history ------------------------------------------------
    def batch_dir(self, label: str) -> Path:
        return self.history_dir / label

    def load_summary(self, label: str) -> Optional[dict]:
        try:
            return json.loads((self.batch_dir(label) / "summary.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_summaries(self) -> list[dict]:
        out = []
        if self.history_dir.exists():
            for d in self.history_dir.iterdir():
                if d.is_dir():
                    s = self.load_summary(d.name)
                    if s:
                        out.append(s)
        return sorted(out, key=lambda s: _version_num(s.get("label", "")))

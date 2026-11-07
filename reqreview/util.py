"""Small helpers shared by every module (no heavy imports)."""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def clock(ts: str) -> str:
    """HH:MM:SS part of an ISO timestamp."""
    return ts[11:19] if len(ts) >= 19 else ts


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_int(text: str) -> int:
    """Deterministic int from text (Python's hash() is salted per process)."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def atomic_write_text(path: Path | str, text: str) -> None:
    """Write via a temp file + os.replace so readers never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: another process has the file open for a moment
            time.sleep(0.1)
    os.replace(tmp, path)
